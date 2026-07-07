"""Video Studio — unified UI for Clean + Edit + Export.
http://localhost:5000

One server. Three phases via tabs:
  CLEAN  — gap/silence/retake removal (was cleaner_app.py)
  EDIT   — auto-plan + render preview + chat refine (was editor_app.py)
  EXPORT — final 1080p render + download
"""
from __future__ import annotations
import json, re, hashlib, subprocess, threading, uuid, shutil, time, os, socket, sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

# Windows' default console codepage (cp1252) can't represent the unicode
# (→, ✓, emoji) every pipeline script prints in its status lines. Set this
# in OUR OWN process env so every subprocess we spawn — render.sh, transcribe.py,
# fetch_logo.py, all of them — inherits it automatically, regardless of how
# they're invoked. Hit this exact crash class repeatedly; fixing it once here
# instead of chasing it into more individual scripts.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

SKILL = Path(__file__).resolve().parent.parent
VENV_PY = SKILL / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python3")
TRANSCRIBE = SKILL / "scripts/transcribe.py"
RENDER_SH = SKILL / "scripts/render.sh"


def _resolve_bash() -> str:
    """render.sh needs GIT BASH, not the bare "bash" on PATH. On Windows,
    C:\\Windows\\System32\\bash.exe is a WSL launcher stub — if it resolves
    before Git's bin (which happens when this process is started via the
    desktop shortcut instead of a dev shell), every render fails with
    "wsl: Failed to translate ..." because WSL tries to mount-translate
    Windows env vars that mean nothing inside a Linux VM. Explicitly prefer
    known Git Bash install locations before falling back to PATH lookup."""
    if os.name != "nt":
        return "bash"
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if Path(candidate).exists():
            return candidate
    # PATH fallback, but skip the WSL stub under System32 if it's what PATH finds
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return "bash"  # last resort — will surface the WSL error clearly if hit


BASH = _resolve_bash()
WORK_ROOT = Path.home() / ".cache/video-edit"
WORK_ROOT.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR = WORK_ROOT / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

REMOTION_STUDIO_PORT = 5057
TUNER_PORT = 5058

SESSIONS: dict[str, dict] = {}


def _run(cmd, **kw): return subprocess.run(cmd, **kw)


def combine_clips(paths: list[str], dest: Path, sess=None) -> Path:
    """Concat multiple raw clips into one mp4. Uses filter_complex concat so
    differing resolutions / framerates are normalized.  Returns dest path."""
    if len(paths) == 1:
        # single clip — just copy/symlink
        shutil.copy(paths[0], dest)
        return dest
    # build filter graph
    parts, labels = [], []
    for i, p in enumerate(paths):
        parts.append(f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                     f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}]")
        parts.append(f"[{i}:a]aresample=async=1:first_pts=0[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(paths)}:v=1:a=1[outv][outa]")
    cmd = ["ffmpeg", "-y"]
    for p in paths: cmd += ["-i", p]
    cmd += ["-filter_complex", ";".join(parts),
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k", str(dest)]
    # Same full-video re-encode as stabilize/camera_movement/splice — same
    # looks-frozen-with-no-feedback bug on multiple/long clips.
    total_sec = sum(probe_duration(Path(p)) for p in paths)
    run_ffmpeg_progress(cmd, str(dest.parent), total_sec, sess, f"Combining {len(paths)} clips")
    return dest


def workdir_for(src: Path) -> Path:
    digest = hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12]
    d = WORK_ROOT / f"{src.stem[:40]}_{digest}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def edit_workdir_for(sess: dict) -> Path:
    """Prefer an explicitly-set edit_workdir (a loaded project snapshot's
    workdir was physically copied to a project-local path that can't be
    re-derived from clean_path) — otherwise always COMPUTE workdir_for the
    clean video, rather than falling back to sess["workdir"] (the Clean
    stage's own transcribe workdir, hashed from the PRE-cut source path —
    a different directory entirely). That wrong fallback silently required
    job_edit() ("Auto-edit") to actually run once — the only place that used
    to set edit_workdir — before /chat, /plan/*, or /plan/render worked at
    all: sending a chat prompt or loading a hand-authored plan before ever
    clicking Auto-edit wrote prompt_queue.json / read broll_plan.json from
    the WRONG directory, and Final export rendered against a stale one too.
    Computing it fresh here means the Clean stage's own pre-seeded
    words.json (see keep_transcript_cache_fresh) is immediately reachable —
    a plan can be authored straight from the transcript with no heuristic
    Auto-edit pass or its throwaway first render ever required."""
    if sess.get("edit_workdir"):
        return Path(sess["edit_workdir"])
    return workdir_for(Path(sess.get("clean_path") or sess["src"]))


def keep_transcript_cache_fresh(out: Path):
    """clean.mp4 (`out`) can be rewritten AFTER job_clean_finish already
    pre-seeded words.json into workdir_for(out) — job_audio_only_finish's
    audio-only reclean and job_apply_camera_effects both call
    apply_audio_filter() again later, which rewrites `out` and bumps its
    mtime. Neither changes word timings at all (audio filters and
    stabilize/camera_movement are video/audio processing, not cuts), but the
    bumped mtime on `out` alone was enough to fail transcribe()'s
    `words.json mtime >= src mtime` freshness check — silently forcing a
    full whisper re-transcription on the next Edit run, discarding whatever
    the user corrected in the transcript editor. Touching words.json's own
    mtime here keeps the cache hit valid without re-deriving anything."""
    words_json = workdir_for(out) / "words.json"
    if words_json.exists():
        os.utime(words_json, None)


def probe_duration(src: Path) -> float:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return float(r.stdout.strip())


def probe_dimensions(src: Path) -> tuple[int, int]:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(src)],
             capture_output=True, text=True, encoding="utf-8", errors="replace")
    w, h = r.stdout.strip().split("x")
    return int(w), int(h)


def detect_silences(src: Path, noise_db: float, min_gap: float):
    r = _run(["ffmpeg", "-i", str(src),
              "-af", f"silencedetect=noise={noise_db}dB:duration={min_gap}",
              "-f", "null", "-"], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    sil, cur = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m: cur = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and cur is not None:
            sil.append((cur, float(m.group(1)))); cur = None
    return sil


def remap_words_through_keeps(words, keeps):
    """After splicing a video by concatenating `keeps` segments (in original
    source-video time) into one continuous output, a word's absolute
    timestamp in the ORIGINAL source needs to be re-expressed as a
    timestamp in the NEW (post-cut) output — segments before it in time got
    physically removed, shifting everything after them earlier. A word
    that fell inside a segment that got cut no longer exists in the output
    at all and is dropped. Words that straddle a kept/cut boundary are also
    dropped (rare — cut boundaries land in silence, not mid-word) rather
    than emitted with a corrupted duration.

    This is what lets a transcript correction made during Clean (before
    cutting) survive into the post-cut transcript used for captions/editing
    — instead of the downstream stage re-transcribing the cut video from
    scratch and losing the correction, we just carry the already-correct
    text through the same time transform the video itself went through."""
    out = []
    offset = 0.0
    for (a, b) in keeps:
        for w in words:
            if w["start"] >= a and w["end"] <= b:
                out.append({**w, "start": round(w["start"] - a + offset, 3),
                            "end": round(w["end"] - a + offset, 3)})
        offset += (b - a)
    out.sort(key=lambda w: w["start"])
    return out


def find_silences_from_transcript(words, total_duration: float, min_gap: float):
    """Derive silence gaps directly from word-level ASR timestamps instead
    of raw audio amplitude thresholding (detect_silences/silencedetect).
    Amplitude thresholding asks "is the audio quiet?" — a question that
    depends entirely on the recording's background noise level, and breaks
    down whenever ambient noise sits above whatever threshold was picked
    (the exact failure mode this project kept hitting). This asks "is the
    SPEAKER talking?" instead — WhisperX's forced-alignment already
    determined precise word start/end times from the actual speech content,
    so any span with no word in it is silence with respect to the speaker,
    independent of how loud the room is. No noise floor involved at all.
    Returns gaps at least `min_gap` seconds long, as (start, end) tuples."""
    if not words: return []
    gaps = []
    prev_end = 0.0
    for w in words:
        if w["start"] - prev_end >= min_gap:
            gaps.append((prev_end, w["start"]))
        prev_end = max(prev_end, w["end"])
    if total_duration - prev_end >= min_gap:
        gaps.append((prev_end, total_duration))
    return gaps


def auto_min_gap_from_words(words) -> float:
    """Determine what counts as a 'cuttable' pause from THIS speaker's own
    natural cadence, instead of one fixed duration applied to everyone.
    Different people speak with different natural pause lengths between
    phrases — a fixed threshold either cuts into someone's normal rhythm
    (too short) or misses most of a fast talker's real pauses (too long,
    the exact problem hit earlier today: 0.3s fixed vs. this speaker's
    actual ~0.1s pauses).

    Computes the gap before every word in the transcript, takes the
    median as "this speaker's typical short pause," and sets the cut
    threshold at 1.5x that — long enough that normal speaking rhythm
    survives untouched, short enough to catch pauses distinctly longer
    than the speaker's own baseline. Purely derived from this video's own
    transcript; no fixed absolute duration is assumed up front."""
    if len(words) < 3: return 0.12
    gaps = [words[i]["start"] - words[i - 1]["end"] for i in range(1, len(words))]
    gaps = [g for g in gaps if g > 0]
    if not gaps: return 0.12
    gaps.sort()
    median = gaps[len(gaps) // 2]
    return max(0.08, median * 1.5)


def auto_noise_db(src: Path, min_gap: float, total_duration: float) -> float:
    """Calibrate the silencedetect threshold to THIS recording's actual
    background noise floor, instead of using one fixed default for every
    upload. A fixed -32dB works for quiet studio audio, but on a noisier
    recording (room tone, fan, ambient hum) the audio never drops that low
    even during real pauses — silencedetect then finds ZERO gaps and the
    "clean" step silently does nothing about silence, which reads as
    completely broken from the outside even though nothing crashed.

    Sweeps thresholds from strictest (quietest-required) to loosest, and
    picks the STRICTEST one that still finds a plausible amount of silence
    for spoken content (roughly 3-40% of total duration) — stricter is
    safer (less likely to misclassify real speech as a gap), so we only
    loosen the threshold as far as this recording's noise floor forces us
    to. Falls back to the old -32dB default if nothing in the sweep looks
    reasonable (e.g. a near-silent or already-tightly-cut source)."""
    for db in (-40, -36, -32, -30, -28, -26, -24, -22, -20, -18):
        sil = detect_silences(src, db, min_gap)
        covered = sum(e - s for s, e in sil)
        frac = covered / max(total_duration, 1.0)
        if 0.03 <= frac <= 0.40:
            return float(db)
    return -32.0


def transcribe(src: Path, wd: Path, sess=None):
    out = wd / "words.json"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return json.loads(out.read_text(encoding="utf-8"))
    cmd = [str(VENV_PY), str(TRANSCRIBE), str(src)]
    if sess is None:
        _run(cmd, check=True)
    else:
        # WhisperX prints no percentage, just phase markers ("[1/2] Extracting
        # audio", "[2/2] Transcribing with WhisperX") -- but on a longform
        # multi-minute video this step alone can run minutes with the OLD
        # blocking _run() call giving zero signal to the UI meanwhile, same
        # looks-frozen bug as stabilize/camera_movement/splice. No frame-count
        # progress is available here, so surface the coarse phase text instead
        # of nothing.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace", bufsize=1)
        tail = []
        for line in proc.stdout:
            line = line.rstrip()
            tail.append(line)
            if len(tail) > 200: tail.pop(0)
            if line:
                sess["transcribe_phase"] = line
        proc.wait()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, output="\n".join(tail))
    skill_wd = Path.home() / ".cache" / "video-edit"
    digest = hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12]
    sw = skill_wd / f"{src.stem[:40]}_{digest}" / "words.json"
    if sw.exists() and sw != out:
        shutil.copy(sw, out)
    return json.loads(out.read_text(encoding="utf-8"))


def pad_span_safe(words, start_idx: int, end_idx: int, pad: float):
    """Pad a cut span [words[start_idx].start, words[end_idx].end] by `pad`
    seconds on each side, but never past the MIDPOINT of the gap to an
    adjacent word. A blind fixed pad bites into a KEPT neighboring word's
    actual spoken audio whenever the natural gap between words is smaller
    than the pad — routine in fast speech (gaps of 0.02-0.05s are common),
    and the exact mechanism behind a kept word surviving a cut half-
    truncated (e.g. "the process is simple" rendering as "the p...")."""
    start = words[start_idx]["start"]
    if start_idx > 0:
        gap = max(0.0, words[start_idx]["start"] - words[start_idx - 1]["end"])
        start -= min(pad, gap / 2)
    else:
        start -= pad
    end = words[end_idx]["end"]
    if end_idx + 1 < len(words):
        gap = max(0.0, words[end_idx + 1]["start"] - words[end_idx]["end"])
        end += min(pad, gap / 2)
    else:
        end += pad
    return (start, end)


def find_repeated_phrases(words):
    """Detect repeated word n-grams anywhere in the word stream — not just
    back-to-back repeats — catching failure modes the pause-based sentence
    chunker in find_retakes() can't see:
      1. Same-breath retakes: speaker messes up and immediately redoes the
         line with near-zero pause (<0.4s), so the pause-chunker never
         splits them into separate "sentences" to compare in the first
         place.
      2. Rambled/jumbled retakes: speaker retries a line 3-4 times with NO
         pause anywhere and DIFFERENT filler/partial content interleaved
         between attempts (e.g. "Work is very simple. You just have to put
         raw video and edit it. Work is very simple. Work is very simple.
         You just have to put raw video.") — the repeated phrase isn't
         adjacent to itself, so a simple consecutive-run scan misses it.
      3. ASR hallucination loops: Whisper getting stuck repeating the same
         short phrase 10-30+ times back-to-back on silence/unclear audio —
         a known failure mode, not a real retake. These get cut ENTIRELY
         (not "keep the last one") since none of the repeats are real
         speech.
    Runs in two stages:

    STAGE A — hallucination loops. A tight, immediately-consecutive run of
    4+ repeats of a short (<=4 word) phrase with near-zero gaps is ASR
    noise, not a real retake — cut in full, all occurrences. This MUST run
    before Stage B: a long loop of e.g. 56 repeats, if left to Stage B's
    general "keep last" matcher, gets fragmented into many different
    "longest common n-gram, keep the last occurrence of THIS n-gram"
    decisions (since many overlapping n-gram alignments exist inside a
    periodic repeat), each of which "keeps" a different small span —
    leaving several stray repeats of pure noise behind instead of removing
    all of it.

    STAGE B — general non-adjacent duplicate-phrase dedup, for genuine
    retakes: scans n-gram lengths from longest to shortest, hashes every
    phrase's occurrence positions, groups occurrences into clusters by time
    gap, and for each cluster with 2+ occurrences keeps the LAST one
    (assumed to be the final take) and cuts the rest. Catches same-breath
    retakes (near-zero pause, so the pause-based sentence chunker in
    find_retakes() never splits them into separate chunks to compare) and
    rambled retakes (speaker retries a line 3-4 times with different filler
    interleaved between attempts, so the repeat isn't even adjacent).

    Stage B runs in multiple passes: a single greedy longest-first pass can
    make a locally-correct but globally-wrong call — a long phrase match
    spans occurrence A (early) and occurrence C (late) and "keeps" C, while
    a shorter phrase match between an intermediate occurrence B and C never
    fires because C was already claimed as "kept" by the first decision, so
    B wrongly survives even though C supersedes it too. Re-running the scan
    lets a still-unclaimed B get re-compared against the previously-kept C,
    since only CUT spans persist across passes — "kept" claims are local to
    one pass.

    Returns cut spans as (start, end) tuples."""
    # Some ASR output emits punctuation as its own standalone word entry
    # (e.g. a lone "," between two otherwise-adjacent repeats). Left in,
    # its stripped-to-empty token breaks back-to-back adjacency checks,
    # since the real content words are no longer exactly index-adjacent
    # even though the TIME gap between them is near-zero. Dropping
    # punctuation-only entries up front (they carry no content) fixes this
    # at the root, and every remaining entry keeps its true timestamp.
    words = [w for w in words if w["word"].strip(".,!?\"'") != ""]
    n = len(words)
    if n < 2: return []
    toks = [w["word"].lower().strip(".,!?\"'") for w in words]

    HALLUCINATION_MIN_RUN = 4
    HALLUC_MAX_GLEN = 4
    HALLUC_GAP_LIMIT = 1.0  # seconds; tight back-to-back only

    claimed = [False] * n

    # ── Stage A ──────────────────────────────────────────────────────────
    i = 0
    while i < n:
        best = None  # (phrase_len, run_count, run_end_idx)
        for plen in range(1, HALLUC_MAX_GLEN + 1):
            if i + plen > n: break
            phrase = toks[i:i + plen]
            run = 1
            pos = i + plen
            while pos + plen <= n:
                if words[pos]["start"] - words[pos - 1]["end"] > HALLUC_GAP_LIMIT: break
                if toks[pos:pos + plen] != phrase: break
                run += 1; pos += plen
            if run >= 2 and (best is None or (plen, run) > (best[0], best[1])):
                best = (plen, run, pos)
        if best and best[1] >= HALLUCINATION_MIN_RUN:
            _plen, _run, end_idx = best
            for k in range(i, end_idx): claimed[k] = True
            i = end_idx
        else:
            i += 1

    # ── Stage B ──────────────────────────────────────────────────────────
    MIN_NGRAM, MAX_NGRAM = 3, min(12, n)
    MAX_GAP_SEC = 20.0  # occurrences farther apart than this aren't the same retake attempt
    MAX_PASSES = 3

    cut_word_idx: set[int] = {k for k in range(n) if claimed[k]}

    for _pass in range(MAX_PASSES):
        pass_claimed = [i in cut_word_idx for i in range(n)]
        pass_cuts: list[tuple[int, int]] = []  # (start_idx, glen)

        for glen in range(MAX_NGRAM, MIN_NGRAM - 1, -1):
            seen: dict[tuple, list[int]] = {}
            for i in range(0, n - glen + 1):
                if any(pass_claimed[i:i + glen]): continue
                phrase = tuple(toks[i:i + glen])
                seen.setdefault(phrase, []).append(i)
            for phrase, idxs in seen.items():
                if len(idxs) < 2: continue
                idxs.sort()
                clusters = [[idxs[0]]]
                for idx in idxs[1:]:
                    if any(pass_claimed[idx:idx + glen]): continue
                    prev_end = clusters[-1][-1] + glen - 1
                    gap = words[idx]["start"] - words[prev_end]["end"]
                    if gap <= MAX_GAP_SEC:
                        clusters[-1].append(idx)
                    else:
                        clusters.append([idx])
                for cluster in clusters:
                    if len(cluster) < 2: continue
                    for idx in cluster[:-1]:
                        if any(pass_claimed[idx:idx + glen]): continue
                        pass_cuts.append((idx, glen))
                        for k in range(idx, idx + glen): pass_claimed[k] = True
                    last_idx = cluster[-1]
                    for k in range(last_idx, last_idx + glen): pass_claimed[k] = True

        if not pass_cuts:
            break
        for idx, glen in pass_cuts:
            cut_word_idx.update(range(idx, idx + glen))

    if not cut_word_idx: return []
    cuts = []
    idxs_sorted = sorted(cut_word_idx)
    run_start = prev = idxs_sorted[0]
    for idx in idxs_sorted[1:]:
        if idx == prev + 1:
            prev = idx; continue
        cuts.append(pad_span_safe(words, run_start, prev, 0.05))
        run_start = prev = idx
    cuts.append(pad_span_safe(words, run_start, prev, 0.05))
    return cuts


def find_retakes(words):
    if not words: return []
    from difflib import SequenceMatcher
    cuts = list(find_repeated_phrases(words))
    # stutter — immediate word-level repeat ("the the")
    for i in range(len(words) - 1):
        a = words[i]["word"].lower().strip(".,!?\"'")
        b = words[i+1]["word"].lower().strip(".,!?\"'")
        if a == b and (words[i+1]["start"] - words[i]["end"]) < 0.8 and len(a) >= 2:
            cuts.append(pad_span_safe(words, i, i, 0.04))

    # Sentence retakes. Word-level ASR output carries no punctuation, so
    # "sentences" here are pause-delimited phrase chunks — a proxy, not a
    # real sentence boundary. A mid-thought breath pause fragments one
    # spoken sentence into several of these chunks, which is why matching
    # needs to look across a TIME window rather than a fixed chunk count:
    # the old version only looked 3 chunks ahead, so a retake cluster with
    # more than ~3 pause-fragments between attempts (very easy to hit with
    # 4-5 re-recordings of the same line) went undetected. It also stopped
    # after the first pairwise match, so long retake chains (3+ attempts)
    # only got partially cut.
    sentences, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"]) > 0.4:
            sentences.append(cur); cur = []
        cur.append(w)
    if cur: sentences.append(cur)

    def tokens_of(sent):
        return [w["word"].lower().strip(".,!?\"'") for w in sent]

    n = len(sentences)
    used = set()
    clusters: list[list[int]] = []
    RETAKE_WINDOW_SEC = 12.0  # how far ahead a re-attempt can land, wall-clock
    MIN_TOKENS = 3
    MATCH_RATIO = 0.55  # required on BOTH sides, not just the earlier sentence —

    for i in range(n):
        if i in used: continue
        a_tokens = tokens_of(sentences[i])
        if len(a_tokens) < MIN_TOKENS: continue
        cluster = [i]
        anchor_tokens = a_tokens
        anchor_end_time = sentences[i][-1]["end"]
        j = i + 1
        while j < n:
            if sentences[j][0]["start"] - anchor_end_time > RETAKE_WINDOW_SEC:
                break
            if j in used:
                j += 1; continue
            b_tokens = tokens_of(sentences[j])
            matched = False
            if len(b_tokens) >= MIN_TOKENS:
                sm = SequenceMatcher(None, anchor_tokens, b_tokens, autojunk=False)
                lo = sm.find_longest_match(0, len(anchor_tokens), 0, len(b_tokens))
                ratio_a = lo.size / len(anchor_tokens)
                ratio_b = lo.size / len(b_tokens)
                # requiring the match on BOTH sides (not just ratio_a, as
                # before) stops a short new sentence that merely shares a
                # few words with a much longer earlier one from falsely
                # tripping the retake detector and cutting real content.
                matched = lo.size >= MIN_TOKENS and ratio_a >= MATCH_RATIO and ratio_b >= MATCH_RATIO
            if matched:
                cluster.append(j)
                # re-anchor on the newest match so wording drift across a
                # long chain of retakes doesn't lose the thread
                anchor_tokens = b_tokens
                anchor_end_time = sentences[j][-1]["end"]
            j += 1
        if len(cluster) > 1:
            clusters.append(cluster)
            used.update(cluster)

    # Cut every sentence in a cluster EXCEPT the last — the final take is
    # kept, everything before it in that retake cluster is removed.
    word_idx_by_id = {id(w): i for i, w in enumerate(words)}
    for cluster in clusters:
        for idx in cluster[:-1]:
            s = sentences[idx]
            start_idx = word_idx_by_id[id(s[0])]
            end_idx = word_idx_by_id[id(s[-1])]
            cuts.append(pad_span_safe(words, start_idx, end_idx, 0.05))

    if not cuts: return []
    cuts.sort()
    merged = [list(cuts[0])]
    for s, e in cuts[1:]:
        if s <= merged[-1][1] + 0.1: merged[-1][1] = max(merged[-1][1], e)
        else: merged.append([s, e])
    return [tuple(m) for m in merged]


def build_keeps(total, silences, retake_cuts, target_gap, cut_head, cut_tail,
                 head_buffer_sec: float = 0.0, tail_buffer_sec: float = 0.0):
    expanded = [[s, e] for s, e in silences]
    for rs, re_ in retake_cuts:
        merged = False
        for s in expanded:
            if s[0] - 0.5 <= rs <= s[1] + 0.5 or s[0] - 0.5 <= re_ <= s[1] + 0.5:
                s[0] = min(s[0], rs); s[1] = max(s[1], re_); merged = True
        if not merged: expanded.append([rs, re_])
    expanded.sort()
    m = []
    for s in expanded:
        if m and s[0] <= m[-1][1]: m[-1][1] = max(m[-1][1], s[1])
        else: m.append(list(s))
    keeps, prev = [], 0.0
    n = len(m)
    for i, (ss, se) in enumerate(m):
        is_head = i == 0 and ss <= 0.1 and cut_head
        is_tail = i == n - 1 and se >= total - 0.1 and cut_tail
        contains_retake = any(ss - 0.1 <= rs and re_ <= se + 0.1 for rs, re_ in retake_cuts)
        if is_head:
            # Trim the head silence but leave head_buffer_sec of it right
            # before speech starts, instead of cutting flush to the first
            # word — a hard flush cut reads as an abrupt, jarring cold
            # start with zero breathing room.
            prev = max(ss, se - head_buffer_sec); continue
        if is_tail:
            # Same idea at the tail: keep tail_buffer_sec of pause after
            # the last word instead of cutting off the instant speech ends.
            tail_keep_end = min(se, ss + tail_buffer_sec)
            if tail_keep_end > prev: keeps.append((prev, tail_keep_end))
            prev = se; break
        if contains_retake:
            if ss > prev: keeps.append((prev, ss))
            prev = se; continue
        seg_end = ss + min(se - ss, target_gap)
        if seg_end > prev: keeps.append((prev, seg_end))
        prev = se
    if prev < total: keeps.append((prev, total))
    return keeps


def build_audio_chain(denoise_i: float, enhance_i: float, noise_floor_db: float | None = None,
                       volume_pct: float = 100.0) -> str:
    """Compose ffmpeg audio filter chain. Each intensity 0-1.
    denoise: RNNoise + spectral afftdn. enhance: EQ + compress + loudness norm.

    afftdn's `nf` parameter tells it "signal at or below this dB level is
    noise, suppress it." The old fixed -20 to -35dB range was blind to the
    actual recording — on audio whose real background noise sits LOUDER
    than -35dB (very common: room tone, fan, traffic), nf ends up STRICTER
    (quieter) than the real noise floor, so afftdn never recognizes the
    actual noise as noise at all and silently does nothing. Anchoring nf to
    the recording's measured noise floor (noise_floor_db — the same
    calibrated silencedetect threshold used for silence-cutting, see
    auto_noise_db()) instead of a blind fixed range fixes this the same way
    silence detection was fixed: measure, don't assume."""
    filters = []
    if denoise_i > 0.02:
        if noise_floor_db is not None:
            # Sit right at the measured floor at low intensity (gentle —
            # only strip what's clearly noise), ranging further above it
            # (more aggressive, catches more of the signal as noise) as
            # intensity increases. Capped so high intensity on a very loud
            # noise floor can't start eating real speech.
            nf = min(noise_floor_db + 1 + 7 * denoise_i, -8)
        else:
            nf = -20 - 15 * denoise_i  # -20 to -35, old fixed fallback
        filters.append(f"afftdn=nf={int(round(nf))}:nt=w")
    if enhance_i > 0.02:
        # Low-end rumble cut. Previously used `equalizer=f=80:t=h:g=...` —
        # but ffmpeg's `equalizer` filter is a narrow peaking/notch filter
        # whose `width` defaults to 1 (in whatever unit `t` sets) when not
        # given explicitly. That line never set `w=`, so it was cutting a
        # ~1Hz-wide sliver at 80Hz — inaudible, effectively a no-op the
        # whole time. `highpass` is the correct tool for broadband rumble/
        # room-tone removal below a cutoff, not a peaking EQ. Cutoff rises
        # with intensity so more aggressive settings remove more low end.
        # Ceiling was way too aggressive at intensity=1 — ratio=4:1
        # compression + a 5dB presence boost + dynaudnorm all stacked at
        # max is heavy enough to produce audible "pumping" (loudness
        # visibly riding up and down as the compressor/normalizer react),
        # which is very plausibly what gets described as "pitch not
        # constant" by ear even though it's a dynamics artifact, not an
        # actual frequency-domain pitch shift. "Enhance" should read as
        # polish at any intensity, not as obvious processing. Gentler
        # ceiling across the board; dynaudnorm's own aggressiveness now
        # scales with intensity too instead of being fixed regardless of
        # the slider position.
        hp_freq = int(50 + 40 * enhance_i)  # 50Hz to 90Hz cutoff
        boost_db = round(1.5 + 2.0 * enhance_i, 1)  # +1.5 to +3.5
        ratio = round(1.5 + 1.0 * enhance_i, 1)  # 1.5 to 2.5
        dyn_p = round(0.5 + 0.35 * enhance_i, 2)  # 0.5 to 0.85 (dynaudnorm max gain change)
        filters.append(f"highpass=f={hp_freq}")
        filters.append(f"equalizer=f=4000:t=q:w=2:g={boost_db}")
        filters.append(f"acompressor=threshold=-18dB:ratio={ratio}:attack=5:release=80")
        filters.append(f"dynaudnorm=p={dyn_p}:m=10:s=12")
    if abs(volume_pct - 100.0) > 0.5:
        filters.append(f"volume={volume_pct / 100.0:.3f}")
    return ",".join(filters) if filters else "anull"


def splice_trim_concat(src, keeps, raw_out: Path, sess=None):
    """The expensive part of the old splice() — trim+concat, video
    re-encoded, audio carried through UNFILTERED — now its own step (was
    splice()'s internal pass 1) so job_clean_finish() can run
    stabilize/camera_movement on THIS output before the audio filter runs,
    instead of on the raw pre-cut upload (see apply_stabilize_and_camera_movement
    for why that reorder matters). Writes directly to raw_out; the caller
    decides what that path means downstream.

    A hard atrim cut lands wherever the cut math says to, almost never
    exactly on a waveform zero-crossing — so concatenating raw trimmed
    segments leaves an abrupt amplitude discontinuity at every single cut
    point, heard as a click/pop. With dozens of cuts (silences + retakes)
    across a video that adds up to constant audible "glitching." A very
    short (8ms, inaudible as an actual fade) in/out fade on each segment
    eliminates the discontinuity at zero perceptible cost to speech.
    """
    FADE = 0.008
    parts, labels = [], []
    for i, (a, b) in enumerate(keeps):
        dur = b - a
        fade = min(FADE, dur / 4)
        parts.append(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}]")
        parts.append(
            f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade},afade=t=out:st={max(0.0, dur - fade)}:d={fade}[a{i}]"
        )
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(keeps)}:v=1:a=1[outv][outa]")
    # Total is the KEPT-timeline duration (the concat output), not the
    # source's, since that's what ffmpeg's own progress `time=` counts up
    # towards here.
    total_sec = sum(b - a for a, b in keeps)
    run_ffmpeg_progress(["ffmpeg", "-y", "-i", str(src), "-filter_complex", ";".join(parts),
          "-map", "[outv]", "-map", "[outa]",
          "-c:v", "libx264", "-preset", "fast", "-crf", "20",
          "-c:a", "aac", "-b:a", "192k", str(raw_out)], str(raw_out.parent), total_sec, sess, "Cutting & splicing video")


def apply_audio_filter(raw: Path, out: Path, audio_filter: str = "anull"):
    """Pass 2 of splice() — cheap: video stream copied untouched, only
    audio gets (re-)encoded through the filter chain. Safe to call
    repeatedly against the same `raw` intermediate with a different
    filter each time (e.g. the user adjusting a denoise slider) without
    ever compounding a previous filter pass."""
    args = ["ffmpeg", "-y", "-i", str(raw)]
    if audio_filter and audio_filter != "anull":
        args += ["-filter:a", audio_filter]
    args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)]
    _run(args, check=True)


def extract_waveform(src: Path, samples: int = 400) -> list[float]:
    """Extract amplitude-per-bucket for visualization. Returns list of 0-1 floats."""
    dur = probe_duration(src)
    sr = max(50, int(samples / max(dur, 1)))  # samples per second
    out = subprocess.run([
        "ffmpeg", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr),
        "-f", "s16le", "-"
    ], check=False, capture_output=True)
    import struct, array
    raw = out.stdout
    if not raw: return []
    # parse as int16 little endian
    arr = array.array("h"); arr.frombytes(raw)
    if not arr: return []
    chunk = max(1, len(arr) // samples)
    peaks = []
    for i in range(0, len(arr), chunk):
        s = arr[i:i+chunk]
        if not s: break
        peaks.append(max(abs(x) for x in s) / 32768.0)
    return peaks[:samples]


def detect_clicks(src: Path, words: list[dict] | None = None) -> list[tuple[float, float]]:
    """Detect short pops/clicks: very short loud transients (silencedetect inverse).
    Heuristic: silences at very tight threshold pick out non-silence segments,
    short ones (< 0.15s) flanked by silence on both sides = likely click.

    This is amplitude-only and has no idea what speech is — a real short
    word ("a", "is", "it" are routinely under 0.15s) matches the exact
    same shape as a click and would otherwise get cut as if it were noise.
    Cross-checking against the transcript (words, when available) so any
    candidate that actually overlaps real spoken word timing is skipped —
    this is very likely the cause of "clicks removal conflicts with
    retakes": a short word near a retake boundary getting double-flagged
    and cut by both systems independently."""
    sil = detect_silences(src, -20, 0.05)
    clicks = []
    for i in range(len(sil) - 1):
        gap_start = sil[i][1]
        gap_end = sil[i+1][0]
        if 0 < gap_end - gap_start < 0.15:
            overlaps_word = words and any(
                w["start"] < gap_end and w["end"] > gap_start for w in words
            )
            if overlaps_word:
                continue
            clicks.append((gap_start - 0.02, gap_end + 0.02))
    return clicks


# ───────────── PHASE 2: rule-based plan generator ─────────────
BRAND_PATTERN = re.compile(
    r"\b(claude|chatgpt|gpt-?4|openai|anthropic|chrome|google|stripe|figma|notion|"
    r"kickbacks|cursor|github|vercel|youtube|instagram|tiktok|twitter|linkedin|"
    r"reddit|discord|slack|zoom|spotify|netflix|apple|microsoft|meta|tesla|nvidia|"
    r"amazon|shopify)\b", re.I)
NUMBER_PATTERN = re.compile(
    r"\b(\$\d+(?:[.,]\d+)*[kKmMbB]?|\d+%|\d+x|"
    r"\d+\s?(?:bucks|dollars|million|billion|hundred|thousand|months?|years?|days?|hours?|minutes?))\b", re.I)


def generate_plan(words):
    if not words: return []
    duration = words[-1]["end"]
    full = " ".join(w["word"] for w in words)
    plan = []
    first_words = " ".join(w["word"] for w in words[:6]).upper().strip(".,!?\"'")
    title = first_words[:18] if len(first_words) > 6 else "WATCH THIS"
    plan.append({
        "kind": "hook_title", "start_sec": 0.0,
        "end_sec": min(2.6, duration * 0.08),
        "kicker": "INTRO", "title": title, "vertical": 0.78,
        "reason": "Cold-open hook"
    })
    used = [(0.0, plan[0]["end_sec"])]
    def fits(s, e):
        if e > duration - 1.0: return False
        return all(e <= us or s >= ue for us, ue in used)

    stat_n = 0
    for m in NUMBER_PATTERN.finditer(full):
        if stat_n >= 2: break
        val = m.group(1).upper().replace(" ", "")
        tok = val.lower().strip("$%").split()[0]
        for w in words:
            wt = w["word"].lower().strip(".,!?\"'$%")
            if wt == tok or wt.startswith(tok):
                s = w["start"] - 0.2; e = min(w["start"] + 3.0, duration - 1.0)
                if fits(s, e):
                    plan.append({"kind": "stat_punch", "start_sec": round(s, 2),
                                 "end_sec": round(e, 2), "value": val,
                                 "caption": "HERO NUMBER", "speech_anchor": w["word"],
                                 "reason": "Hero number"})
                    used.append((s, e)); stat_n += 1
                break

    brands, seen = [], set()
    for i, w in enumerate(words):
        wt = w["word"].lower().strip(".,!?\"'")
        if BRAND_PATTERN.fullmatch(wt) and wt not in seen:
            seen.add(wt); brands.append((i, wt))
        if len(brands) >= 4: break
    if len(brands) >= 2:
        s = max(words[brands[0][0]]["start"] - 0.3, 0)
        e = min(words[brands[-1][0]]["end"] + 1.5, duration - 1.0)
        if fits(s, e):
            items = []
            for idx, b in brands:
                lf = "google_chrome.png" if b == "chrome" else f"{b}.png"
                items.append({"image_path": lf, "label": b.upper(), "appear_sec": round(words[idx]["start"], 2)})
            items[-1]["accent"] = True
            plan.append({"kind": "tool_logo_burst", "start_sec": round(s, 2),
                         "end_sec": round(e, 2), "vertical": 0.45, "items": items,
                         "speech_anchor": brands[0][1], "reason": "Brands named"})
            used.append((s, e))

    cre = re.compile(r"\b(instead|but|however|actually|honestly)\b", re.I)
    wp_n = 0
    for w in words:
        if wp_n >= 2: break
        if cre.fullmatch(w["word"].strip(".,!?\"'")):
            s = w["start"] - 0.1; e = min(s + 2.5, duration - 1.0)
            if fits(s, e):
                idx = words.index(w)
                phrase = " ".join(x["word"] for x in words[idx:idx+4]).strip(".,")
                plan.append({"kind": "word_pop", "start_sec": round(s, 2),
                             "end_sec": round(e, 2), "vertical": 0.78,
                             "items": [{"text": "{" + phrase[:28] + "}", "appear_sec": round(s + 0.1, 2), "accent": True}],
                             "speech_anchor": w["word"], "reason": "Pivot moment"})
                used.append((s, e)); wp_n += 1

    sub_s = max(duration - 2.5, duration * 0.92); sub_e = duration - 0.05
    plan.append({"kind": "subscribe", "start_sec": round(sub_s, 2),
                 "end_sec": round(sub_e, 2), "vertical": 0.88, "reason": "CTA close"})
    plan.sort(key=lambda b: b["start_sec"])
    return plan


# NOTE: render.sh's workdir used to be guessed (Windows bash vs Python hash
# the same path differently), so this module used to shotgun-copy the plan
# into every guessed variant "just in case". That's gone now — every render
# call passes STUDIO_WORKDIR explicitly (see BASH invocations below), so
# render.sh always looks in the exact right place. The old guess-and-copy
# function was deleted: variant #1 of its guesses was literally the same
# formula as workdir_for(), so it would silently overwrite a DIFFERENT
# session's plan any time two sessions shared a source video — real data
# loss, not just wasted work.


def append_chat(sid, role, text, typ="msg"):
    sess = SESSIONS[sid]
    sess.setdefault("chat", []).append({"role": role, "text": text, "type": typ, "ts": time.time()})


def queue_path(sid: str) -> Path | None:
    """Per-session prompt queue, beside the plan in the edit workdir. Claude
    reads this file to apply natural-language fixes the UI can't do itself."""
    sess = SESSIONS.get(sid)
    if not sess: return None
    wd = edit_workdir_for(sess)
    return wd / "prompt_queue.json" if wd else None


def enqueue_prompt(sid: str, text: str) -> Path | None:
    qp = queue_path(sid)
    if not qp: return None
    try:
        queue = json.loads(qp.read_text(encoding="utf-8")) if qp.exists() else []
    except Exception:
        queue = []
    queue.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "prompt": text, "consumed": False})
    qp.parent.mkdir(parents=True, exist_ok=True)
    qp.write_text(json.dumps(queue, indent=2, ensure_ascii=True), encoding="ascii")
    return qp


# ───────────── Jobs ─────────────
def apply_camera_movement(src: Path, out: Path, mode: str, intensity: float = 0.5, sess=None):
    """CapCut's "AI Movement Tracking" — NOT subject-tracking, NOT
    stabilization (confirmed directly from CapCut's own tool page): it
    adds SYNTHETIC camera motion (zoom/shake/soft/dynamic) to footage that
    doesn't have real camera movement, to make a locked-off shot feel more
    alive. Implemented as a crop-then-scale-back-up trick: crop a window
    smaller than the source frame with a time-varying position/size, then
    scale that window back up to the original resolution — the crop
    window's movement over time IS the "camera movement." All four modes
    are real ffmpeg video filters, no external model.

    zoom  — crop window steadily shrinks (i.e. picture appears to zoom in)
    shake — crop window's position jitters on independent sine waves per
            axis (different frequency/phase so it doesn't look like a
            simple circle) — simulated handheld camera
    soft  — same idea as zoom but much gentler amplitude and slower, for
            a barely-there ambient drift
    dynamic — NOT implemented here; see follow_subject_camera() below,
            which drives the crop window from actual tracked face
            position instead of a synthetic sine/linear function

    IMPORTANT: crop's w/h here are TIME-VARYING (zoom mode shrinks the
    crop window over the clip), which means the cropped frame size
    itself changes frame to frame — but a video stream must have a
    CONSTANT frame size to encode at all. The fix is scaling back up to
    the literal original resolution (probed once, hardcoded numbers),
    NOT `scale=iw:ih` — `iw`/`ih` after a crop refer to the crop's own
    (still-varying) output size, so that would just carry the same
    varying-size problem one step further instead of fixing it. Confirmed
    directly: this was the actual cause of zoom's ffmpeg failure, not an
    eval-mode issue (crop in this ffmpeg build has no `eval` option at
    all — an earlier, wrong theory)."""
    probe = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=width,height", "-of", "csv=p=0", str(src)],
                 capture_output=True, text=True, check=True)
    orig_w, orig_h = (int(x) for x in probe.stdout.strip().split(",")[:2])

    if mode == "zoom":
        # crop genuinely cannot animate w/h at all (confirmed directly —
        # ffmpeg errors "Error reinitializing filters" the instant crop's
        # own output size would change frame to frame, since a video
        # stream must have constant dimensions). scale CAN grow per-frame
        # (it has an eval=frame option crop doesn't), so the zoom effect
        # is: scale the image progressively larger, then crop a FIXED
        # orig_w x orig_h window from the center of that now-larger frame
        # — crop's own w/h here are plain literals, satisfying the
        # constant-output-size requirement, while the growing scale
        # upstream is what actually produces the zoom-in.
        grow = 0.15 * max(0.05, intensity)
        sw = f"iw*(1+{grow}*t/{{dur}})"
        sh = f"ih*(1+{grow}*t/{{dur}})"
        vf = f"scale=w='{sw}':h='{sh}':eval=frame,crop={orig_w}:{orig_h}"
    elif mode == "shake":
        margin = 0.06 * max(0.05, intensity)  # how much crop margin the shake can use
        cw = f"iw*(1-{margin*2})"
        ch = f"ih*(1-{margin*2})"
        cx = f"(iw-out_w)/2 + iw*{margin}*sin(2*PI*1.3*t)"
        cy = f"(ih-out_h)/2 + ih*{margin}*sin(2*PI*1.7*t+1)"
        vf = f"crop=w='{cw}':h='{ch}':x='{cx}':y='{cy}',scale={orig_w}:{orig_h}"
    elif mode == "soft":
        margin = 0.025 * max(0.05, intensity)
        cw = f"iw*(1-{margin*2})"
        ch = f"ih*(1-{margin*2})"
        cx = f"(iw-out_w)/2 + iw*{margin}*sin(2*PI*0.12*t)"
        cy = f"(ih-out_h)/2 + ih*{margin*0.6}*sin(2*PI*0.09*t+0.7)"
        vf = f"crop=w='{cw}':h='{ch}':x='{cx}':y='{cy}',scale={orig_w}:{orig_h}"
    else:
        raise ValueError(f"unknown camera movement mode: {mode!r}")

    total_sec = probe_duration(src)
    if mode == "zoom":
        vf = vf.format(dur=max(0.1, total_sec))
    # Per-frame scale/crop expressions on 4K source make this MUCH slower
    # than a plain encode (observed ~0.18x realtime) with zero feedback
    # otherwise — same frozen-looking "nothing happening" symptom stabilize
    # had before run_ffmpeg_progress existed; reusing it here too.
    run_ffmpeg_progress(["ffmpeg", "-y", "-i", str(src), "-vf", vf,
          "-c:a", "copy", "-c:v", "libx264", "-preset", "fast", "-crf", "20", str(out)],
          str(out.parent), total_sec, sess, f"Applying camera movement ({mode})")


FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")


def run_ffmpeg_progress(cmd, cwd, total_sec, sess=None, phase="processing"):
    """Stream an ffmpeg subprocess's stderr and parse its own
    `time=HH:MM:SS.ms` line into sess["render_progress"] as it goes —
    reuses the exact {phase,current,total,eta_sec} shape
    run_render_streaming() already fills in for Remotion renders, so the
    frontend's existing progressBarHtml() shows it with no separate UI
    needed. Two-pass ffmpeg stabilization (vidstabdetect + vidstabtransform)
    on a full-length video takes real time with subprocess.run's default
    buffered output giving zero feedback until the whole pass finishes —
    this is why "stabilizing" looked frozen. sess=None just runs ffmpeg
    silently (equivalent to the old _run(cmd, check=True))."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace", bufsize=1)
    start = time.time()
    tail = []
    for line in proc.stderr:
        tail.append(line)
        if len(tail) > 200: tail.pop(0)
        if sess is None: continue
        m = FFMPEG_TIME_RE.search(line)
        if m:
            cur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            elapsed = time.time() - start
            rate = cur / elapsed if elapsed > 0 and cur > 0 else 0
            eta = (total_sec - cur) / rate if rate > 0 else None
            sess["render_progress"] = {"phase": phase, "current": round(min(cur, total_sec)),
                                        "total": round(total_sec),
                                        "eta_sec": round(eta) if eta is not None else None}
    proc.wait()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output="".join(tail))


def stabilize_video(src: Path, out: Path, sess=None):
    """Two-pass ffmpeg stabilization via libvidstab (already compiled into
    this machine's ffmpeg — verified before building this). Pass 1
    (vidstabdetect) analyzes camera motion across frames and writes a
    transform file; pass 2 (vidstabtransform) applies the inverse motion
    to smooth it out, plus a mild unsharp pass since stabilization softens
    the image slightly. zoom=0 avoids auto-cropping tighter into the
    speaker's face than necessary; optzoom=1 lets it pick the minimum zoom
    that still avoids showing empty/black frame edges.

    vidstabdetect/vidstabtransform's result=/input= options take the
    transform-file path INSIDE the -vf filtergraph string, where colon
    and backslash are reserved syntax characters (colon separates filter
    options, backslash escapes). A raw Windows path breaks the parser at
    the drive-letter colon — confirmed directly (ffmpeg errors
    "No option name near..." at exactly that point) and several escaping
    attempts (backslash-escaping the colon, forward-slashing the path,
    wrapping in quotes) all still failed the same way. The reliable fix:
    sidestep colon-in-filtergraph-value parsing entirely by using a
    relative filename with cwd set to its directory — verified working.

    Both passes stream their own progress into sess["render_progress"]
    (via run_ffmpeg_progress) when a session dict is passed in — a
    two-pass full-video ffmpeg run took real wall-clock time with zero
    visible feedback otherwise, which read as a frozen/hung "stabilizing"
    status in the UI.

    The severe posterized/"melting" pixel corruption reported against 4K
    portrait footage (2160x3840) is a resolution-triggered bug in this
    ffmpeg build's libvidstab, not a tuning problem — confirmed by testing
    every vidstabtransform parameter individually (shakiness, accuracy,
    zoom/optzoom, interpol, unsharp amount) against real frames: the exact
    same corruption appeared regardless of which one changed, but running
    the identical filter chain on the same footage downscaled to 1080x1920
    produced a clean frame every time. Fix: run both vidstab passes on a
    downscaled copy (analysis and transform must match resolution — vidstab
    stores motion in absolute pixel units, not normalized) and scale back
    up to the source's original dimensions afterward so output size is
    unchanged. `'min(1080,iw)':-2` only downscales sources actually above
    1080 wide and keeps height even (libx264 requirement)."""
    transforms_name = out.name + ".trf"
    workdir = str(out.parent)
    total_sec = probe_duration(src)
    orig_w, orig_h = probe_dimensions(src)
    safe_vf = "scale='min(1080,iw)':-2"
    run_ffmpeg_progress(["ffmpeg", "-y", "-i", str(src), "-vf",
          f"{safe_vf},vidstabdetect=shakiness=8:accuracy=15:result={transforms_name}",
          "-f", "null", "-"], workdir, total_sec, sess, "Stabilizing — analyzing motion (pass 1/2)")
    # unsharp stays mild since downscale-then-upscale softens the image a
    # touch; crf18/preset slow preserves quality on this once-per-session step
    # (splice()'s routine cut pass uses crf20/preset fast for speed instead).
    run_ffmpeg_progress(["ffmpeg", "-y", "-i", str(src), "-vf",
          f"{safe_vf},vidstabtransform=input={transforms_name}:zoom=0:optzoom=1:smoothing=15:interpol=bicubic,"
          f"unsharp=3:3:0.5:3:3:0,scale={orig_w}:{orig_h}",
          "-c:v", "libx264", "-preset", "slow", "-crf", "18",
          "-c:a", "copy", str(out)], workdir, total_sec, sess, "Stabilizing — smoothing & encoding (pass 2/2)")
    (out.parent / transforms_name).unlink(missing_ok=True)


def apply_combine(sid) -> bool:
    """Combine multi-source clips into one continuous file — this still has
    to run before transcription/silence-detection, which need one file to
    analyze in the first place. Stabilize and camera_movement used to run
    here too, but now run AFTER splice instead (see
    apply_stabilize_and_camera_movement) — they're the two expensive
    per-frame re-encodes in the whole pipeline, and a typical raw recording
    is mostly silence/retakes that splice() throws away a few steps later;
    processing that footage before the cut was pure wasted render time (and
    for camera_movement's zoom specifically, actively wrong-looking — its
    per-frame growth is a function of time-since-start-of-clip, so applying
    it before the cut made the final exported zoom jump discontinuously
    between surviving segments instead of growing smoothly).

    Always rebuilds `j["src"]` forward from `j["orig_src"]` (the true
    original upload, captured once and never overwritten) rather than
    mutating `j["src"]` in place. Returns False (and sets an error status)
    if combine failed; caller should stop immediately in that case.
    """
    j = SESSIONS[sid]
    if "orig_src" not in j:
        j["orig_src"] = j["src"]
    cur = Path(j["orig_src"])

    sources = j.get("sources") or [j["orig_src"]]
    if len(sources) > 1:
        already_combined = j.get("combined_path")
        if already_combined and Path(already_combined).exists():
            cur = Path(already_combined)
        else:
            j["status"] = "clean:combining"
            j["render_progress"] = None
            first = Path(sources[0])
            combined = first.parent / f"{first.stem}.combined.mp4"
            try:
                combine_clips(sources, combined, sess=j)
                cur = combined
                j["combined_path"] = str(combined)
            except Exception as e:
                import traceback
                j["status"] = "clean:error"; j["error"] = f"combine failed: {e}"
                j["trace"] = traceback.format_exc()
                log_error(sid, "combine", str(e))
                return False

    j["src"] = str(cur)
    return True


def apply_stabilize_and_camera_movement(sid, src: Path) -> Path | None:
    """Stabilize + camera-movement, run on the SPLICED (already-cut) video
    rather than the raw upload — see apply_combine's docstring for why this
    reorder matters. Always runs fresh against whatever `src` it's given
    (no cross-reclean caching here, unlike combine): the input is a new cut
    every time job_clean_finish() reaches this point at all, since a pure
    audio tweak (the common no-video-change reclean) takes the cheaper
    job_audio_only_finish() path instead and never calls this. Returns the
    final processed path, or None with j["status"]/error already set on
    failure."""
    j = SESSIONS[sid]
    cur = src

    if j.get("stabilize"):
        j["status"] = "clean:stabilizing"
        j["render_progress"] = None
        stab_out = cur.parent / f"{cur.stem}.stab.mp4"
        try:
            stabilize_video(cur, stab_out, sess=j)
        except Exception as e:
            import traceback
            j["status"] = "clean:error"; j["error"] = f"stabilize failed: {e}"
            j["trace"] = traceback.format_exc()
            log_error(sid, "stabilize", str(e))
            return None
        cur = stab_out

    camera_mode = j.get("camera_movement", "none")
    if camera_mode and camera_mode != "none":
        j["status"] = "clean:camera_movement"
        # Clear whatever the PREVIOUS stage (e.g. splicing, or stabilize
        # above) left in render_progress -- otherwise this shows a stale
        # "100% done" bar the whole time this stage is genuinely still
        # starting, which reads as more broken than no bar at all.
        j["render_progress"] = None
        try:
            cam_out = cur.parent / f"{cur.stem}.cam.mp4"
            if camera_mode == "dynamic":
                # dynamic_camera.py does two slow things with no progress of
                # their own: (1) a per-frame OpenCV read/crop/write loop at
                # full source resolution -- much slower than ffmpeg's native
                # loop -- then (2) an internal ffmpeg remux+scale (scale
                # added so dynamic's output resolution matches the other
                # modes) whose own stderr flows through this same captured
                # stream since dynamic_camera.py doesn't redirect it --
                # parse BOTH phases' progress lines rather than only the
                # first, since a fixed "100%" for 2+ minutes during phase 2
                # is exactly the looks-frozen bug this exists to fix.
                frame_re = re.compile(r"^frame (\d+)/(\d+)$")
                total_sec = probe_duration(cur)
                proc = subprocess.Popen(
                    [str(VENV_PY), str(SKILL / "scripts/dynamic_camera.py"),
                     str(cur), str(cam_out), "--zoom", "0.85", "--smoothing", "0.85"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1)
                start = time.time()
                tail = []
                for line in proc.stdout:
                    line = line.rstrip()
                    tail.append(line)
                    if len(tail) > 200: tail.pop(0)
                    elapsed = time.time() - start
                    m = frame_re.match(line)
                    if m:
                        fcur, ftot = int(m.group(1)), int(m.group(2))
                        rate = fcur / elapsed if elapsed > 0 and fcur > 0 else 0
                        eta = (ftot - fcur) / rate if rate > 0 else None
                        j["render_progress"] = {"phase": "Applying camera movement (dynamic) — cropping",
                                                 "current": fcur, "total": ftot,
                                                 "eta_sec": round(eta) if eta is not None else None}
                        continue
                    m = FFMPEG_TIME_RE.search(line)
                    if m:
                        tcur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                        rate = tcur / elapsed if elapsed > 0 and tcur > 0 else 0
                        eta = (total_sec - tcur) / rate if rate > 0 else None
                        j["render_progress"] = {"phase": "Applying camera movement (dynamic) — encoding",
                                                 "current": round(min(tcur, total_sec)), "total": round(total_sec),
                                                 "eta_sec": round(eta) if eta is not None else None}
                proc.wait()
                if proc.returncode != 0:
                    raise subprocess.CalledProcessError(proc.returncode, "dynamic_camera.py", output="\n".join(tail))
            else:
                apply_camera_movement(cur, cam_out, camera_mode,
                                       intensity=j.get("camera_movement_intensity", 0.5), sess=j)
        except Exception as e:
            import traceback
            j["status"] = "clean:error"; j["error"] = f"camera movement failed: {e}"
            j["trace"] = traceback.format_exc()
            log_error(sid, "camera_movement", str(e))
            return None
        if cur != src:
            cur.unlink(missing_ok=True)  # drop the now-consumed stabilize intermediate
        cur = cam_out

    return cur


def job_clean(sid):
    """Stage 1: transcribe only, then STOP and wait for the user to review/
    correct the transcript. Splitting this out (rather than transcribing
    and immediately splicing in one shot) exists because a wrong word in
    the transcript silently propagates into captions and retake detection
    — better to catch it before the render happens than to re-render after
    noticing. See job_clean_finish() for the actual cut/splice stage,
    triggered by /clean/approve once the user is happy with the transcript."""
    j = SESSIONS[sid]
    if not apply_combine(sid):
        return
    j["status"] = "clean:transcribing"
    src = Path(j["src"])
    wd = workdir_for(src); j["workdir"] = str(wd)
    try:
        words = transcribe(src, wd, sess=j)
        j["words"] = words  # expose for transcript editor
        j["status"] = "clean:awaiting_approval"
    except Exception as e:
        import traceback
        j["status"] = "clean:error"; j["error"] = str(e); j["trace"] = traceback.format_exc()
        log_error(sid, "clean", str(e))


def job_clean_finish(sid):
    """Stage 2: analyze (silence/retake detection, using whatever the user
    has corrected in the transcript editor by now), splice, THEN
    stabilize/camera_movement, then the audio filter. Triggered by
    /clean/approve AND by /clean/reclean — the latter is why apply_combine()
    runs here too: multi-source combine must already be applied before
    analyzing, in case this is reached via reclean without a fresh
    job_clean() first. stabilize/camera_movement live in THIS function
    (after splice) rather than in preprocessing, specifically so they run on
    the shorter, already-cut footage instead of the full raw upload — see
    apply_stabilize_and_camera_movement's docstring."""
    j = SESSIONS[sid]
    if not apply_combine(sid):
        return
    src = Path(j["src"])
    words = j.get("words", [])
    try:
        j["status"] = "clean:analyzing"
        total = probe_duration(src)
        # Silence is derived from the TRANSCRIPT (gaps between spoken
        # words), not raw audio amplitude — see find_silences_from_transcript
        # for why: amplitude thresholding depends on the recording's
        # background noise level and breaks down whenever the room isn't
        # near-silent during pauses. min_gap itself is calibrated to this
        # speaker's own natural pause length, not a fixed number applied to
        # everyone. j["min_gap"]/j["noise_db"] are kept for the transcript
        # editor's waveform view and the denoise audio filter respectively,
        # not for deciding what counts as a cuttable silence.
        effective_min_gap = auto_min_gap_from_words(words) if words else j["min_gap"]
        silences = find_silences_from_transcript(words, total, effective_min_gap)
        retake_cuts = find_retakes(words) if j["remove_retakes"] else []
        click_cuts = detect_clicks(src, words) if j.get("remove_clicks") else []
        manual_cuts = j.get("manual_cuts", [])  # from transcript editor
        extra_cuts = retake_cuts + click_cuts + manual_cuts
        j["status"] = "clean:splicing"
        j["render_progress"] = None
        keeps = build_keeps(total, silences, extra_cuts, j["target_gap"], j["cut_head"], j["cut_tail"],
                             head_buffer_sec=j.get("head_buffer_sec", 0.0),
                             tail_buffer_sec=j.get("tail_buffer_sec", 0.0))
        # snapshot for undo
        j.setdefault("history", []).append({
            "manual_cuts": list(manual_cuts),
            "noise_db": j["noise_db"], "min_gap": j["min_gap"], "target_gap": j["target_gap"],
            "cut_head": j["cut_head"], "cut_tail": j["cut_tail"],
            "head_buffer_sec": j.get("head_buffer_sec", 0.5), "tail_buffer_sec": j.get("tail_buffer_sec", 0.5),
            "remove_retakes": j["remove_retakes"], "remove_clicks": j.get("remove_clicks", False),
            "denoise_intensity": j.get("denoise_intensity", 0),
            "enhance_intensity": j.get("enhance_intensity", 0),
        })
        if len(j["history"]) > 20: j["history"] = j["history"][-20:]

        audio_filter = build_audio_chain(j.get("denoise_intensity", 0), j.get("enhance_intensity", 0),
                                          noise_floor_db=j.get("noise_db"), volume_pct=j.get("volume_pct", 100.0))
        out = src.parent / f"{src.stem}.clean.mp4"
        raw = Path(str(out) + ".raw.mp4")
        # Base cut only, stopping here — stabilize/camera_movement are a
        # separate, explicit step (POST /clean/apply_camera_effects,
        # job_apply_camera_effects) the user triggers on THIS result once
        # they've reviewed it, not bundled into every Clean/Re-clean.
        splice_trim_concat(src, keeps, raw, sess=j)
        apply_audio_filter(raw, out, audio_filter)
        j["clean_path"] = str(out)

        # job_edit() re-transcribes on `out` (clean_path) rather than `src`,
        # which hashes to a DIFFERENT workdir (workdir_for() hashes the
        # resolved file path). Left alone, that stage would re-transcribe
        # the cut video from scratch — throwing away any transcript
        # correction made just now, and reintroducing whatever the raw ASR
        # got wrong in the first place. Pre-seed that exact workdir with
        # the already-corrected transcript, remapped through the cut
        # timeline, so transcribe()'s mtime-cache check finds it fresh and
        # skips re-transcription entirely.
        try:
            remapped = remap_words_through_keeps(words, keeps)
            out_wd = workdir_for(out)
            (out_wd / "words.json").write_text(
                json.dumps(remapped, indent=2, ensure_ascii=True), encoding="ascii")
        except Exception:
            pass  # non-fatal — worst case job_edit re-transcribes as before
        j["clean_original"] = total
        j["clean_new"] = sum(b - a for a, b in keeps)
        j["clean_silences"] = len(silences)
        j["clean_retakes"] = len(retake_cuts)
        j["clean_clicks"] = len(click_cuts)
        j["clean_manual"] = len(manual_cuts)
        j["status"] = "clean:done"
    except Exception as e:
        import traceback
        j["status"] = "clean:error"; j["error"] = str(e); j["trace"] = traceback.format_exc()
        log_error(sid, "clean", str(e))


def job_audio_only_finish(sid):
    """Cheap re-run for a pure denoise/enhance tweak — reapplies just the
    audio filter to the raw (unfiltered) intermediate saved by the last full
    clean, with the video stream copied untouched instead of re-encoded.
    `raw` here is whatever the video currently is — the plain cut
    (job_clean_finish's output) if camera effects were never applied, or the
    stabilize/camera_movement-processed version if job_apply_camera_effects
    already ran and swapped it in (that function keeps this exact filename
    so this path never needs to know or care which one it's looking at).
    Triggered by /clean/reclean when it detects the only changed settings
    are denoise_intensity/enhance_intensity."""
    j = SESSIONS[sid]
    try:
        out = Path(j["clean_path"])
        raw = Path(str(out) + ".raw.mp4")
        if not raw.exists():
            # No raw intermediate to work from (e.g. a session started
            # before this split existed) — fall back to a full re-clean.
            job_clean_finish(sid)
            return
        j["status"] = "clean:splicing"
        audio_filter = build_audio_chain(j.get("denoise_intensity", 0), j.get("enhance_intensity", 0),
                                          noise_floor_db=j.get("noise_db"), volume_pct=j.get("volume_pct", 100.0))
        apply_audio_filter(raw, out, audio_filter)
        keep_transcript_cache_fresh(out)
        j["status"] = "clean:done"
    except Exception as e:
        import traceback
        j["status"] = "clean:error"; j["error"] = str(e); j["trace"] = traceback.format_exc()
        log_error(sid, "clean_audio_only", str(e))


def job_apply_camera_effects(sid):
    """Explicit, separate step: stabilize/camera_movement applied to the
    ALREADY-CLEANED base video, only when the user asks for it — not
    automatically bundled into Clean/Re-clean. This is the whole point of
    the reorder: review the base cut first, then decide whether synthetic
    camera movement is worth the extra render time on THIS (already short,
    already final-cut) video, rather than committing to it upfront on the
    full raw recording. Triggered by POST /clean/apply_camera_effects.

    Operates on the raw (audio-unfiltered) intermediate, not clean_path
    itself, and overwrites it in place under the SAME filename — so
    job_audio_only_finish()'s cheap path keeps working completely unchanged
    afterward, whether or not this ever ran."""
    j = SESSIONS[sid]
    if not j.get("clean_path"):
        j["status"] = "clean:error"; j["error"] = "No cleaned video yet — run Clean first."
        return
    out = Path(j["clean_path"])
    raw = Path(str(out) + ".raw.mp4")
    if not raw.exists():
        j["status"] = "clean:error"; j["error"] = "Missing raw intermediate — re-run Clean first."
        return
    try:
        processed = apply_stabilize_and_camera_movement(sid, raw)
        if processed is None:
            return  # error status already set
        if processed != raw:
            processed.replace(raw)
        audio_filter = build_audio_chain(j.get("denoise_intensity", 0), j.get("enhance_intensity", 0),
                                          noise_floor_db=j.get("noise_db"), volume_pct=j.get("volume_pct", 100.0))
        apply_audio_filter(raw, out, audio_filter)
        keep_transcript_cache_fresh(out)
        j["status"] = "clean:done"
    except Exception as e:
        import traceback
        j["status"] = "clean:error"; j["error"] = str(e); j["trace"] = traceback.format_exc()
        log_error(sid, "camera_effects", str(e))


RENDER_PROGRESS_RE = re.compile(r"Rendered (\d+)/(\d+)")
ENCODE_PROGRESS_RE = re.compile(r"Encoded (\d+)/(\d+)")


def run_render_streaming(cmd, env, cwd, sess):
    """Same job as subprocess.run(capture_output=True), but reads stdout line
    by line as it arrives instead of buffering until the process exits — so
    render.sh's "Rendered N/1033" / "Encoded N/1033" lines update sess in
    real time instead of only being visible after the whole render finishes.
    Returns an object with .returncode and .output (joined tail), same shape
    the callers already expect from subprocess.run's result."""
    sess["render_progress"] = {"phase": "starting", "current": 0, "total": 0}
    proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    lines = []
    start_time = time.time()
    for line in proc.stdout:
        lines.append(line)
        if len(lines) > 400: lines.pop(0)  # keep a bounded tail for error messages
        m = RENDER_PROGRESS_RE.search(line)
        if m:
            cur, tot = int(m.group(1)), int(m.group(2))
            elapsed = time.time() - start_time
            rate = cur / elapsed if elapsed > 0 and cur > 0 else 0
            eta = (tot - cur) / rate if rate > 0 else None
            sess["render_progress"] = {"phase": "rendering", "current": cur, "total": tot,
                                        "eta_sec": round(eta) if eta is not None else None}
            continue
        m = ENCODE_PROGRESS_RE.search(line)
        if m:
            cur, tot = int(m.group(1)), int(m.group(2))
            sess["render_progress"] = {"phase": "encoding", "current": cur, "total": tot, "eta_sec": None}
    proc.wait()
    sess["render_progress"] = {"phase": "done", "current": 0, "total": 0, "eta_sec": None}

    class Result:
        returncode = proc.returncode
        stdout = "".join(lines)
        stderr = ""
    return Result()


def job_edit(sid):
    j = SESSIONS[sid]
    src_str = j.get("clean_path") or j["src"]
    src = Path(src_str).resolve()
    if not src.exists():
        # try common Downloads location
        candidate = Path.home() / "Downloads" / Path(src_str).name
        if candidate.exists(): src = candidate.resolve()
    if not src.exists():
        j["status"] = "edit:error"; j["error"] = f"File not found: {src_str}. Use full absolute path."
        append_chat(sid, "assistant", f"**Error:** File not found at `{src_str}`. Paste the full absolute path (e.g. `C:\\Users\\DELL\\Downloads\\video.clean.mp4`).", "error")
        return
    j["clean_path"] = str(src)
    wd = workdir_for(src); j["edit_workdir"] = str(wd)
    j["status"] = "edit:planning"
    try:
        words = transcribe(src, wd, sess=j)
        plan = generate_plan(words)
        write_json_atomic(wd / "broll_plan.json", plan)
        j["plan"] = plan
        append_chat(sid, "assistant", f"**Plan generated** — {len(plan)} beats: " + ", ".join(b['kind'] for b in plan), "plan")
        j["status"] = "edit:rendering"
        append_chat(sid, "assistant", "Rendering preview (~30s)...", "status")
        # NOTE: used to unconditionally delete /tmp/video-edit-render.lock*
        # here "just in case" — that's wrong. render.sh's own lock is already
        # PID-aware and self-healing (steals a stale lock automatically); this
        # code was blindly deleting it even when a DIFFERENT render was still
        # legitimately holding it, letting two renders run concurrently and
        # race on the same broll_plan.json / words.json via the in-place
        # rewrite scripts (align_to_speech.py etc) — the actual cause of the
        # repeated 0-byte plan corruption. Just let render.sh's own lock work.
        env = {**dict(os.environ), "FORCE_RENDER": "1", "ASPECT": SESSIONS[sid].get("aspect", "auto"),
               "STUDIO_WORKDIR": str(wd)}
        r = run_render_streaming([BASH, str(RENDER_SH), str(src)], env, str(SKILL), j)
        if r.returncode != 0:
            tail = (r.stdout or "")[-600:]
            j["status"] = "edit:error"; j["error"] = f"render exit {r.returncode}"
            log_error(sid, "render", tail)
            append_chat(sid, "assistant", f"**Render failed** (exit {r.returncode}). See ⚠ error log for details.", "error")
            return
        preview = src.parent / f"{src.stem}.preview.mp4"
        if preview.exists():
            j["preview_path"] = str(preview); j["status"] = "edit:done"
            append_chat(sid, "assistant", "**✅ Preview ready.** Open Remotion Studio + Tuner to adjust, or render Final.", "done")
    except Exception as e:
        import traceback
        j["status"] = "edit:error"; j["error"] = str(e); j["trace"] = traceback.format_exc()
        log_error(sid, "edit", str(e))


def job_final(sid):
    j = SESSIONS[sid]
    j["status"] = "export:rendering"
    src = Path(j.get("clean_path") or j["src"]).resolve()
    if not src.exists():
        cand = Path.home() / "Downloads" / Path(j.get("clean_path") or j["src"]).name
        if cand.exists(): src = cand.resolve()
    try:
        wd = edit_workdir_for(j)
        env = {**dict(os.environ), "QUALITY": "final", "FORCE_RENDER": "1",
               "ASPECT": SESSIONS[sid].get("aspect", "auto"), "STUDIO_WORKDIR": str(wd)}
        r = run_render_streaming([BASH, str(RENDER_SH), str(src)], env, str(SKILL), j)
        if r.returncode != 0:
            j["status"] = "export:error"; j["error"] = f"render exit {r.returncode}: {(r.stdout or '')[-600:]}"
            log_error(sid, "export", j["error"]); return
        final = src.parent / f"{src.stem}.enhanced.mp4"
        if final.exists():
            j["final_path"] = str(final); j["status"] = "export:done"
            save_project(sid)  # auto-save on export
    except Exception as e:
        j["status"] = "export:error"; j["error"] = str(e)


# ───────────── Plan editor helpers ─────────────
PLAN_HISTORY: dict[str, list] = {}  # sid -> [plan_snapshot, ...]
PLAN_FUTURE: dict[str, list] = {}

DEFAULT_BEAT_FIELDS = {
    "hook_title": {"kicker": "KICKER", "title": "HOOK", "vertical": 0.78},
    "word_pop": {"vertical": 0.78, "items": [{"text": "{NEW}", "appear_sec": 0, "accent": True}]},
    "stat_punch": {"value": "$0", "caption": "CAPTION"},
    "quote_pull": {"quote_text": "Quote text", "attribution": ""},
    "image_card": {"image_path": "broll/placeholder.jpg", "card_top": 0.60, "card_fit": "wide"},
    "tool_logo_burst": {"vertical": 0.45, "items": [{"image_path": "anthropic.png", "label": "BRAND", "appear_sec": 0}]},
    "bar_overlay": {"title": "TITLE", "vertical": 0.55, "bars": [{"label": "A", "value": 50, "display": "50%"}, {"label": "B", "value": 50, "display": "50%"}]},
    "subscribe": {"vertical": 0.88},
    "bullet_burst": {"items": [{"text": "Bullet 1", "appear_sec": 0}, {"text": "Bullet 2", "appear_sec": 0}]},
    "portrait_burst": {"items": [{"image_path": "broll/face.jpg", "label": "Name", "appear_sec": 0}]},
    "ratio_dots": {"total": 12, "marked": 9, "polarity": "negative", "caption": "ITEMS"},
    "ring_chart": {"title": "TITLE", "holeRatio": 0.6, "segments": [
        {"label": "A", "value": 60}, {"label": "B", "value": 25}, {"label": "C", "value": 15}]},
    "countdown_reveal": {"steps": ["3", "2", "1", "GO"], "subtitle": ""},
    "logo_reveal_hero": {"image_path": "anthropic.png", "name": "BRAND", "tagline": ""},
    "logo_reveal_style": {"image_path": "anthropic.png", "name": "BRAND", "tagline": "", "style": "bounce"},
    "image_compare_slider": {"before_image": "broll/before.jpg", "after_image": "broll/after.jpg",
        "before_label": "BEFORE", "after_label": "AFTER"},
    "end_card": {"title": "THAT'S IT", "subtitle": "", "cta": "SUBSCRIBE"},
    "gallery_grid": {"images": ["broll/1.jpg", "broll/2.jpg", "broll/3.jpg"], "caption": ""},
    "image_carousel": {"images": ["broll/1.jpg", "broll/2.jpg"], "labels": [], "slot_sec": 1.4},
    "image_zoom_reveal": {"image": "broll/placeholder.jpg", "caption": ""},
    "masonry_gallery": {"images": ["broll/1.jpg", "broll/2.jpg", "broll/3.jpg"], "caption": ""},
    "photo_stack": {"images": ["broll/1.jpg", "broll/2.jpg", "broll/3.jpg"], "captions": []},
    "picture_in_picture": {"main_image": "broll/main.jpg", "pip_image": "broll/pip.jpg", "pip_label": ""},
    "polaroid_frame": {"image": "broll/placeholder.jpg", "caption": ""},
    "split_panels": {"left_image": "broll/left.jpg", "right_image": "broll/right.jpg", "left_label": "", "right_label": ""},
    "area_chart": {"title": "TITLE", "chart_points": [{"label": "Jan", "value": 10}, {"label": "Feb", "value": 40}, {"label": "Mar", "value": 65}]},
    "progress_bars": {"title": "TITLE", "bars": [{"label": "A", "value": 80}, {"label": "B", "value": 55}]},
    "stat_delta": {"target": 100, "prefix": "$", "delta_value": "12.5%", "delta_direction": "up", "delta_label": "This Month"},
    "comparison_bars": {"before_label": "BEFORE", "after_label": "AFTER", "comparison_rows": [{"label": "Speed", "before": 30, "after": 90}]},
    "circular_progress": {"value_pct": 75, "title": "PROGRESS"},
    "bounce_title": {"title": "TITLE", "subtitle": ""},
    "bubble_pop_text": {"quote_text": "POP THIS"},
    "pop_text": {"quote_text": "POP THIS"},
    "pulse_text": {"quote_text": "PULSE THIS"},
    "text_sweep": {"quote_text": "highlight these words as read"},
    "typewriter_text": {"quote_text": "typing this out...", "chars_per_second": 12},
    "list_reveal": {"title": "TITLE", "items": [{"text": "Item one"}, {"text": "Item two"}]},
    "card_flip": {"front_text": "FRONT", "back_text": "BACK", "flip_sec": 1.5},
    "notification_stack": {"notifications": [{"app_name": "App", "title": "Title", "body": "Body", "time": "now"}]},
    "carousel_3d": {"title": "TITLE", "carousel_items": [{"label": "A"}, {"label": "B"}, {"label": "C"}]},
    "sound_wave": {"bar_count": 24, "caption": ""},
}

PLAN_TEMPLATES = {
    "hook_stats_cta": {
        "name": "Hook + Stats + CTA",
        "beats_for": lambda dur: [
            {"kind": "hook_title", "start_sec": 0, "end_sec": 2.5, "kicker": "INTRO", "title": "WATCH THIS", "vertical": 0.78},
            {"kind": "stat_punch", "start_sec": dur*0.3, "end_sec": dur*0.4, "value": "100%", "caption": "STAT"},
            {"kind": "stat_punch", "start_sec": dur*0.5, "end_sec": dur*0.6, "value": "$10K", "caption": "STAT"},
            {"kind": "word_pop", "start_sec": dur*0.7, "end_sec": dur*0.78, "vertical": 0.78, "items": [{"text": "{TAKEAWAY}", "appear_sec": dur*0.7, "accent": True}]},
            {"kind": "subscribe", "start_sec": dur-2.5, "end_sec": dur-0.1, "vertical": 0.88},
        ]
    },
    "story_arc": {
        "name": "Story arc (setup → twist → resolution)",
        "beats_for": lambda dur: [
            {"kind": "hook_title", "start_sec": 0, "end_sec": 2.5, "kicker": "STORY", "title": "OPENING", "vertical": 0.78},
            {"kind": "word_pop", "start_sec": dur*0.25, "end_sec": dur*0.32, "vertical": 0.78, "items": [{"text": "{SETUP}", "appear_sec": dur*0.25, "accent": True}]},
            {"kind": "word_pop", "start_sec": dur*0.5, "end_sec": dur*0.58, "vertical": 0.78, "items": [{"text": "{TWIST}", "appear_sec": dur*0.5, "accent": True}]},
            {"kind": "quote_pull", "start_sec": dur*0.75, "end_sec": dur*0.85, "quote_text": "The takeaway line.", "attribution": ""},
            {"kind": "subscribe", "start_sec": dur-2.5, "end_sec": dur-0.1, "vertical": 0.88},
        ]
    },
    "tutorial_steps": {
        "name": "Tutorial steps (3-step walkthrough)",
        "beats_for": lambda dur: [
            {"kind": "hook_title", "start_sec": 0, "end_sec": 2.5, "kicker": "TUTORIAL", "title": "HOW TO", "vertical": 0.78},
            {"kind": "stat_punch", "start_sec": dur*0.2, "end_sec": dur*0.27, "value": "STEP 1", "caption": "FIRST"},
            {"kind": "stat_punch", "start_sec": dur*0.45, "end_sec": dur*0.52, "value": "STEP 2", "caption": "NEXT"},
            {"kind": "stat_punch", "start_sec": dur*0.7, "end_sec": dur*0.77, "value": "STEP 3", "caption": "FINAL"},
            {"kind": "subscribe", "start_sec": dur-2.5, "end_sec": dur-0.1, "vertical": 0.88},
        ]
    },
    "product_demo": {
        "name": "Product demo (problem → solution → CTA)",
        "beats_for": lambda dur: [
            {"kind": "hook_title", "start_sec": 0, "end_sec": 2.5, "kicker": "PROBLEM", "title": "FIX THIS", "vertical": 0.78},
            {"kind": "image_card", "start_sec": dur*0.25, "end_sec": dur*0.4, "image_path": "broll/product.png", "card_top": 0.60, "card_fit": "wide"},
            {"kind": "bar_overlay", "start_sec": dur*0.55, "end_sec": dur*0.7, "title": "BEFORE / AFTER", "vertical": 0.55, "bars": [{"label": "BEFORE", "value": 100, "display": "100%"}, {"label": "AFTER", "value": 20, "display": "20%", "highlight": True}]},
            {"kind": "subscribe", "start_sec": dur-2.5, "end_sec": dur-0.1, "vertical": 0.88},
        ]
    },
}


def get_plan_path(sid: str) -> Path | None:
    sess = SESSIONS.get(sid)
    if not sess: return None
    return edit_workdir_for(sess) / "broll_plan.json"


def load_plan(sid: str) -> list:
    p = get_plan_path(sid)
    if p and p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except: return []
    return []


def write_json_atomic(path: Path, data) -> None:
    """Write-then-rename so a crash or a race with another writer can never
    leave a truncated (0-byte) file — os.replace is atomic on both Windows
    and POSIX. Hit this exact corruption 4 times before adding this."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="ascii")
    os.replace(tmp, path)


def save_plan(sid: str, plan: list, push_history: bool = True):
    p = get_plan_path(sid)
    if not p: return
    if not plan and p.exists() and p.stat().st_size > 2:
        # refuse to silently clobber a real plan with an empty one — this is
        # exactly the failure mode that kept corrupting broll_plan.json
        log_error(sid, "save_plan", "refused to overwrite non-empty plan with []")
        return
    if push_history:
        cur = load_plan(sid)
        PLAN_HISTORY.setdefault(sid, []).append(cur)
        if len(PLAN_HISTORY[sid]) > 30:
            PLAN_HISTORY[sid] = PLAN_HISTORY[sid][-30:]
        PLAN_FUTURE[sid] = []
    write_json_atomic(p, plan)


def list_music_tracks() -> list[str]:
    assets = SKILL / "assets"
    return sorted([f.name for f in assets.glob("*.mp3")])


def list_logos() -> list[str]:
    logos = SKILL / "assets" / "logos"
    return sorted([f.name for f in logos.glob("*.png")])


def fetch_logo_brand(brand: str) -> str | None:
    """Run fetch_logo.py for a brand, return filename if successful."""
    try:
        _run([str(VENV_PY), str(SKILL / "scripts/fetch_logo.py"), brand], check=False, capture_output=True, timeout=30)
    except Exception:
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", brand.lower()).strip("_") + ".png"
    if (SKILL / "assets" / "logos" / slug).exists():
        return slug
    return None


def pexels_search(query: str, portrait: bool = True) -> list[dict]:
    api_key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not api_key:
        # try .env
        try:
            for ln in (SKILL / ".env").read_text().splitlines():
                if ln.startswith("PEXELS_API_KEY="):
                    api_key = ln.split("=", 1)[1].strip()
                    break
        except Exception: pass
    if not api_key: return []
    import urllib.request, urllib.parse
    url = f"https://api.pexels.com/v1/search?query={urllib.parse.quote(query)}&per_page=12&orientation={'portrait' if portrait else 'landscape'}"
    req = urllib.request.Request(url, headers={"Authorization": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
        return [{"id": p["id"], "thumb": p["src"]["medium"], "full": p["src"]["large"], "photographer": p["photographer"]}
                for p in d.get("photos", [])]
    except Exception as e:
        return []


def pexels_download(url: str, sid: str, name: str) -> str | None:
    sess = SESSIONS.get(sid)
    if not sess: return None
    wd = edit_workdir_for(sess)
    broll = wd / "broll"; broll.mkdir(exist_ok=True)
    import urllib.request
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)[:40] + ".jpg"
    dest = broll / safe
    try:
        urllib.request.urlretrieve(url, dest)
        return f"broll/{safe}"
    except Exception:
        return None


def _already_listening(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port)); s.close(); return False
    except OSError:
        s.close(); return True


def launch_studio():
    if _already_listening(REMOTION_STUDIO_PORT): return True  # already running, nothing to do
    npx = shutil.which("npx")  # resolves npx.cmd on Windows — Popen(["npx",...]) can't
    if not npx:
        log_error("system", "launch_studio", "npx not found on PATH"); return False
    try:
        subprocess.Popen([npx, "--no-install", "remotion", "studio", "src/index.ts", "--port", str(REMOTION_STUDIO_PORT)],
                         cwd=str(SKILL / "remotion"), shell=(os.name == "nt"))
        return True
    except Exception as e:
        log_error("system", "launch_studio", str(e)); return False


ERROR_LOG: list[dict] = []  # recent errors across all sessions

def log_error(sid: str, where: str, msg: str):
    ERROR_LOG.append({"ts": time.time(), "sid": sid, "where": where, "msg": str(msg)[:500]})
    if len(ERROR_LOG) > 100: del ERROR_LOG[:-100]


def next_project_name(date_str: str) -> str:
    """CapCut-style dated name: 0207, then 0207 (1), 0207 (2), ... on collision."""
    if not (PROJECTS_DIR / date_str).exists():
        return date_str
    i = 1
    while (PROJECTS_DIR / f"{date_str} ({i})").exists():
        i += 1
    return f"{date_str} ({i})"


def save_project(sid: str) -> Path | None:
    """Snapshot the project into its OWN folder: project.json + thumb.jpg +
    copies of clean/preview/final mp4 + a copy of the whole edit workdir
    (plan, transcript, captions, broll assets). Re-cleaning or re-editing the
    SAME source video later overwrites the shared <stem>.clean.mp4 /
    <stem>.preview.mp4 filenames next to the source — without this snapshot,
    reopening an older saved project would silently show whatever the LATEST
    work produced instead of what was actually saved. This is the fix for
    that: every save is a self-contained folder nothing else can mutate."""
    sess = SESSIONS.get(sid)
    if not sess: return None
    src = Path(sess.get("clean_path") or sess.get("src") or "")
    if not src.exists(): return None
    name = sess.get("_project_name")
    if not name:
        name = next_project_name(time.strftime("%m%d"))
        sess["_project_name"] = name
    proj_dir = PROJECTS_DIR / name
    proj_dir.mkdir(parents=True, exist_ok=True)
    proj = proj_dir / "project.json"
    thumb = proj_dir / "thumb.jpg"

    snapshot = {k: v for k, v in sess.items()
                if k not in ("waveform", "words")}  # heavy, derivable

    for key, fname in (("clean_path", "clean.mp4"), ("preview_path", "preview.mp4"),
                       ("final_path", "final.mp4")):
        p = sess.get(key)
        if p and Path(p).exists():
            dst = proj_dir / fname
            try:
                shutil.copy2(p, dst); snapshot[key] = str(dst)
            except Exception: pass

    live_wd = edit_workdir_for(sess)
    if live_wd.exists() and live_wd.is_dir():
        snap_wd = proj_dir / "workdir"
        try:
            shutil.copytree(live_wd, snap_wd, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.mp4", "*.wav"))
            snapshot["workdir"] = str(snap_wd)
            snapshot["edit_workdir"] = str(snap_wd)
        except Exception: pass

    snapshot["saved_at"] = time.time()
    snapshot["src"] = snapshot.get("clean_path") or str(src)
    snapshot["_plan_history"] = PLAN_HISTORY.get(sid, [])
    snapshot["_plan_future"] = PLAN_FUTURE.get(sid, [])
    proj.write_text(json.dumps(snapshot, indent=2, ensure_ascii=True), encoding="ascii")

    thumb_src = Path(snapshot.get("clean_path") or snapshot.get("src") or "")
    if thumb_src.exists():
        try:
            subprocess.run(["ffmpeg", "-y", "-ss", "1", "-i", str(thumb_src), "-frames:v", "1",
                            "-vf", "scale=320:-1", str(thumb)],
                           check=False, capture_output=True, timeout=15)
        except Exception: pass
    return proj


def load_project(proj_path: Path) -> str | None:
    """Load a saved project, return new sid. All paths in the snapshot already
    point inside the project's own folder — nothing needs regenerating."""
    if not proj_path.exists(): return None
    snap = json.loads(proj_path.read_text(encoding="utf-8"))
    sid = uuid.uuid4().hex
    PLAN_HISTORY[sid] = snap.pop("_plan_history", [])
    PLAN_FUTURE[sid] = snap.pop("_plan_future", [])
    SESSIONS[sid] = snap
    snap["_project_name"] = proj_path.parent.name  # so re-save overwrites, not duplicates
    wd = Path(snap.get("workdir") or "")
    if (wd / "words.json").exists():
        try: SESSIONS[sid]["words"] = json.loads((wd / "words.json").read_text(encoding="utf-8"))
        except: pass
    return sid


def delete_project(proj_path: Path) -> bool:
    try:
        proj_dir = proj_path.parent
        if proj_path.exists() and proj_dir.parent == PROJECTS_DIR:
            shutil.rmtree(proj_dir); return True
    except Exception: pass
    return False


def list_projects() -> list[dict]:
    out = []
    for proj in PROJECTS_DIR.glob("*/project.json"):
        try:
            d = json.loads(proj.read_text(encoding="utf-8"))
            src = d.get("src", "")
            out.append({
                "path": str(proj),
                "name": proj.parent.name,
                "video_name": Path(src).name if src else "",
                "saved_at": d.get("saved_at", 0),
                "status": d.get("status", "?"),
                "thumb": str(proj.parent / "thumb.jpg") if (proj.parent / "thumb.jpg").exists() else None,
            })
        except Exception:
            pass
    out.sort(key=lambda p: -p.get("saved_at", 0))
    return out


def _just_render(sid: str):
    """Re-render preview only (no plan regen). Disk is the source of truth —
    reload it so Claude's queue edits (written straight to broll_plan.json)
    are what gets rendered and snapshotted, not a stale in-memory copy."""
    sess = SESSIONS.get(sid)
    if not sess: return
    sess["plan"] = load_plan(sid)
    sess["status"] = "edit:rendering"
    src = Path(sess.get("clean_path") or sess["src"]).resolve()
    wd = edit_workdir_for(sess)
    # render.sh's own lock is PID-aware and self-healing — don't clear it here
    try:
        env = {**dict(os.environ), "FORCE_RENDER": "1", "ASPECT": SESSIONS[sid].get("aspect", "auto"),
               "STUDIO_WORKDIR": str(wd)}
        r = run_render_streaming([BASH, str(RENDER_SH), str(src)], env, str(SKILL), sess)
        if r.returncode != 0:
            tail = (r.stdout or "")[-800:]
            sess["status"] = "edit:error"
            sess["error"] = f"render exit {r.returncode}: {tail}"
            log_error(sid, "render", sess["error"])
            return
        preview = src.parent / f"{src.stem}.preview.mp4"
        if preview.exists():
            sess["preview_path"] = str(preview)
            sess["status"] = "edit:done"
        else:
            sess["status"] = "edit:error"; sess["error"] = "preview not produced"
            log_error(sid, "render", "render completed but preview file missing")
    except Exception as e:
        sess["status"] = "edit:error"; sess["error"] = str(e)
        log_error(sid, "render", str(e))


_TUNER_PROC = {"proc": None, "workdir": None}


def launch_tuner(workdir: str | None = None):
    # the tuner is a single long-running process pointed at one workdir — if
    # it's already up for a DIFFERENT project, restart it against the current
    # one instead of leaving it silently stale.
    if _TUNER_PROC["proc"] is not None and _TUNER_PROC["workdir"] == workdir \
            and _TUNER_PROC["proc"].poll() is None:
        return True  # already running for this exact project
    if _TUNER_PROC["proc"] is not None and _TUNER_PROC["proc"].poll() is None:
        _TUNER_PROC["proc"].terminate()
        try: _TUNER_PROC["proc"].wait(timeout=3)
        except Exception: pass
    elif _already_listening(TUNER_PORT):
        # something else is bound to this port (e.g. a manual launch) — leave it alone
        return True
    try:
        env = dict(os.environ)
        if workdir: env["VIDEO_STUDIO_WORKDIR"] = workdir
        env["TUNER_PORT"] = str(TUNER_PORT)
        proc = subprocess.Popen([sys.executable, str(SKILL / "scripts/position_tuner.py")], env=env)
        _TUNER_PROC["proc"] = proc; _TUNER_PROC["workdir"] = workdir
        return True
    except Exception as e:
        log_error("system", "launch_tuner", str(e)); return False


HTML_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Video Studio</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,'Segoe UI',sans-serif}
:root{--bg:#0F121A;--bg2:#1E2434;--border:#2a3247;--text:#E9ECED;--muted:#B5BFC2;--dim:#7a8497;--input-bg:#0F121A;--input-border:#343E5B;--accent:#CFFF05;--accent-dark:#0F121A}
body.light{--bg:#F5F6F8;--bg2:#FFFFFF;--border:#E1E4E8;--text:#1a1f2e;--muted:#5B6478;--dim:#9099A8;--input-bg:#F5F6F8;--input-border:#D1D5DB;--accent:#84C500;--accent-dark:#FFFFFF}
body{background:var(--bg);color:var(--text);min-height:100vh;display:grid;grid-template-rows:auto 1fr;height:100vh}
header{padding:12px 24px;background:var(--bg2);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:14px}
header h1{color:var(--accent);font-size:14px;letter-spacing:.08em;text-transform:uppercase;font-weight:900}
.head-tools{display:flex;gap:8px;align-items:center}
.head-tools button{background:transparent;border:1px solid var(--border);color:var(--muted);padding:6px 10px;border-radius:5px;cursor:pointer;font-size:11px;font-weight:600}
.head-tools button:hover{color:var(--accent);border-color:var(--accent)}
.error-badge{background:#5b2a2a;color:#ffb3b3;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700}
.tabs{display:flex;gap:6px}
.tab{padding:8px 18px;border-radius:6px;cursor:pointer;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#7a8497;font-weight:700;border:1px solid transparent}
.tab.active{background:#0F121A;color:#CFFF05;border-color:#343E5B}
.tab:hover{color:#E9ECED}
main{overflow-y:auto;padding:24px 32px}
.wrap{max-width:900px;margin:0 auto}
.card{background:#1E2434;border-radius:10px;padding:22px;margin-bottom:18px;border:1px solid #2a3247}
.card h2{font-size:11px;color:#CFFF05;text-transform:uppercase;letter-spacing:.08em;margin-bottom:14px;font-weight:700}
.drop{border:2px dashed #343E5B;border-radius:12px;padding:36px 20px;text-align:center;cursor:pointer;transition:.15s}
.drop:hover{border-color:#CFFF05}
.drop .b{color:#E9ECED;font-size:15px;font-weight:600;margin-bottom:6px}
.drop .s{color:#B5BFC2;font-size:12px}
.drop input[type=text]{width:100%;background:#0F121A;border:1px solid #343E5B;color:#E9ECED;padding:7px 9px;border-radius:5px;font-family:monospace;font-size:10px;margin-top:12px}
.row{display:grid;grid-template-columns:200px 1fr 70px;gap:12px;align-items:center;padding:8px 0;border-bottom:1px solid #252b3d}
.row:last-child{border:none}
.row label{font-size:12px;color:#D2D8DA}
.row label small{display:block;color:#7a8497;font-size:10px;margin-top:2px}
input[type=range]{accent-color:#CFFF05;width:100%}
input[type=number]{width:64px;background:#0F121A;border:1px solid #343E5B;color:#E9ECED;padding:5px 7px;border-radius:4px;font-family:monospace;font-size:11px}
.toggles{display:flex;flex-direction:column;gap:10px}
.toggle{display:grid;grid-template-columns:1fr auto;gap:12px;background:#0F121A;border:1px solid #343E5B;border-radius:8px;padding:12px 16px;cursor:pointer;align-items:center}
.toggle .lbl{font-size:12px;color:#D2D8DA;font-weight:600}
.toggle .lbl small{display:block;color:#7a8497;font-size:10px;font-weight:400;margin-top:2px}
.switch{position:relative;width:44px;height:24px;background:#343E5B;border-radius:12px;transition:.2s;flex-shrink:0}
.switch::after{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;background:#7a8497;border-radius:50%;transition:.2s}
.switch::before{content:'OFF';position:absolute;right:7px;top:50%;transform:translateY(-50%);font-size:8px;font-weight:800;letter-spacing:.05em;color:#5B6478}
.toggle.on .switch{background:#CFFF05}
.toggle.on .switch::after{left:23px;background:#0F121A}
.toggle.on .switch::before{content:'ON';left:7px;right:auto;color:#0F121A}
.toggle.on .lbl{color:#CFFF05}
.btn{display:block;width:100%;background:#CFFF05;color:#0F121A;border:none;padding:14px;border-radius:8px;font-weight:800;font-size:13px;letter-spacing:.06em;text-transform:uppercase;cursor:pointer;transition:.15s}
.btn:hover{background:#dfff45}
.btn:disabled{background:#343E5B;color:#7a8497;cursor:not-allowed}
.btn.secondary{background:#0F121A;color:#CFFF05;border:1px solid #CFFF05}
.status{padding:14px;background:#0F121A;border-radius:8px;font-size:12px;color:#B5BFC2;font-family:monospace;margin-top:14px}
.status .ok{color:#CFFF05}
.status .err{color:#ff6b6b}
.stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-top:14px}
.stat{background:#0F121A;border-radius:6px;padding:12px;text-align:center}
.stat .v{font-size:20px;color:#CFFF05;font-weight:800;font-family:monospace}
.stat .l{font-size:10px;color:#7a8497;text-transform:uppercase;letter-spacing:.06em;margin-top:4px}
.dl{display:block;text-align:center;padding:14px;background:#CFFF05;color:#0F121A;border-radius:8px;font-weight:800;text-decoration:none;margin-top:14px;text-transform:uppercase;font-size:13px}
.player{max-width:100%;max-height:65vh;width:auto;height:auto;display:block;margin:12px auto 0;border-radius:8px;background:#000}
.actrow{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}
.hidden{display:none}
.chat{max-height:300px;overflow-y:auto;background:#0F121A;border-radius:8px;padding:12px;margin-bottom:12px}
.msg{padding:10px 12px;border-radius:6px;margin-bottom:8px;font-size:12px;line-height:1.5}
.msg.assistant{background:#1E2434;border-left:2px solid #CFFF05}
.msg.user{background:#252b3d;border-left:2px solid #B5BFC2}
.msg strong{color:#CFFF05}
.msg code{background:#0F121A;padding:1px 5px;border-radius:3px;font-family:monospace;font-size:11px}
.chatform{display:grid;grid-template-columns:1fr auto;gap:8px}
.chatform input{background:#0F121A;border:1px solid #343E5B;color:#E9ECED;padding:9px 12px;border-radius:6px;font-size:12px}
.chatform button{background:#CFFF05;color:#0F121A;border:none;padding:9px 16px;border-radius:6px;font-weight:700;cursor:pointer;font-size:11px;text-transform:uppercase}
.preset{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
.preset div{padding:10px;background:#0F121A;border:1px solid #343E5B;border-radius:6px;text-align:center;cursor:pointer;font-size:11px;color:#B5BFC2;font-weight:600;transition:.15s}
.preset div.sel{border-color:#CFFF05;color:#CFFF05}
.qrow{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.qrow label{font-size:11px;color:#D2D8DA;display:block;margin-bottom:4px}
.qrow select{width:100%;background:#0F121A;border:1px solid #343E5B;color:#E9ECED;padding:7px 9px;border-radius:5px;font-size:12px}
.timeline{position:relative;width:100%;height:74px;background:#0F121A;border-radius:8px;overflow:hidden;cursor:crosshair;user-select:none}
.tl-ruler{position:absolute;top:0;left:0;right:0;height:18px;background:#1a2030;border-bottom:1px solid #252b3d;font-size:9px;color:#7a8497;display:flex;align-items:center;padding:0 4px}
.tl-beat{position:absolute;top:22px;height:46px;border-radius:5px;cursor:pointer;overflow:hidden;color:#0F121A;font-weight:700;font-size:10px;padding:4px 6px;text-overflow:ellipsis;white-space:nowrap;transition:border-color .1s;border:2px solid transparent}
.tl-beat.sel{border-color:#fff;box-shadow:0 0 0 1px #fff}
.tl-handle{position:absolute;top:0;width:5px;height:100%;background:rgba(0,0,0,0.3);cursor:ew-resize}
.tl-handle.l{left:0}.tl-handle.r{right:0}
.beat-editor{background:#0F121A;border-radius:8px;padding:14px;margin-top:10px}
.beat-editor h3{font-size:11px;color:#CFFF05;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px}
.beat-row{display:grid;grid-template-columns:100px 1fr;gap:10px;align-items:center;margin-bottom:8px}
.beat-row label{font-size:11px;color:#D2D8DA}
.beat-row input,.beat-row textarea,.beat-row select{width:100%;background:#1E2434;border:1px solid #343E5B;color:#E9ECED;padding:6px 9px;border-radius:5px;font-size:12px;font-family:inherit}
.beat-row textarea{font-family:'Consolas',monospace;font-size:11px;resize:vertical;min-height:50px}
.beat-actions{display:flex;gap:6px;margin-top:10px}
.beat-actions button{background:#343E5B;color:#E9ECED;border:none;padding:6px 10px;border-radius:5px;font-size:10px;cursor:pointer;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.beat-actions button:hover{background:#5B6478}
.beat-actions button.danger{background:#5b2a2a;color:#ffb3b3}
.beat-actions button.danger:hover{background:#7a3838}
.toolrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.toolrow select,.toolrow input,.toolrow button{background:#0F121A;border:1px solid #343E5B;color:#E9ECED;padding:7px 10px;border-radius:5px;font-size:11px;cursor:pointer}
.toolrow button:hover{border-color:#CFFF05}
.toolrow button.primary{background:#CFFF05;color:#0F121A;border-color:#CFFF05;font-weight:700}
.modal{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.8);display:none;align-items:center;justify-content:center;z-index:100}
.modal.show{display:flex}
.modal-body{background:#1E2434;border-radius:12px;padding:24px;max-width:760px;max-height:80vh;overflow-y:auto;width:90%}
.modal-body h3{color:#CFFF05;font-size:14px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px}
.thumbs{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}
.thumbs img{width:100%;height:120px;object-fit:cover;border-radius:5px;cursor:pointer;border:2px solid transparent}
.thumbs img:hover{border-color:#CFFF05}
.music-list{display:flex;flex-direction:column;gap:6px;margin-top:10px}
.music-row{display:grid;grid-template-columns:1fr auto;gap:10px;background:#0F121A;border-radius:5px;padding:8px 12px;align-items:center}
.music-row .nm{font-size:11px;color:#D2D8DA;font-family:monospace}
.music-row audio{height:28px}
</style></head><body>
<header>
  <h1>🎬 Video Studio</h1>
  <div class="tabs">
    <div class="tab" data-tab="dash" onclick="tab('dash')">Projects</div>
    <div class="tab active" data-tab="clean" onclick="tab('clean')">1. Clean</div>
    <div class="tab" data-tab="edit" onclick="tab('edit')">2. Edit</div>
    <div class="tab" data-tab="export" onclick="tab('export')">3. Export</div>
  </div>
  <div class="head-tools">
    <button id="err-btn" onclick="openModal('modal-errors')" title="Error log">⚠ <span id="err-count">0</span></button>
    <button onclick="saveProject()" title="Ctrl+S">💾 Save</button>
    <button onclick="toggleTheme()" title="Toggle theme">🌓</button>
    <button onclick="openModal('modal-tour')" title="Help">?</button>
  </div>
</header>
<main>

<div id="t-dash" class="wrap hidden">
  <div class="card">
    <h2>Past projects</h2>
    <div id="proj-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-top:10px"></div>
    <div id="proj-empty" style="text-align:center;padding:30px;color:var(--dim);font-size:12px;display:none">No saved projects yet. Clean a video, click <strong>💾 Save</strong> to start one.</div>
  </div>
</div>

<div id="t-clean" class="wrap">
  <div class="card">
    <h2>Source video(s)</h2>
    <div class="drop" id="drop">
      <div class="b">Drop one or many videos here</div>
      <div class="s">multiple clips will be combined in order, then cleaned as one</div>
      <input type="file" id="file" accept="video/*" multiple style="display:none">
      <input type="text" id="path" placeholder="C:\Users\...\video.mp4">
    </div>
    <div id="clip-list" style="margin-top:10px;display:flex;flex-direction:column;gap:6px"></div>
  </div>
  <div class="card">
    <h2>Cleaning knobs</h2>
    <div class="row"><label>Silence threshold<small>dB. Lower = more aggressive</small></label>
      <input type="range" id="noise" min="-50" max="-15" step="1" value="-32" oninput="document.getElementById('n-noise').value=this.value">
      <input type="number" id="n-noise" value="-32" oninput="document.getElementById('noise').value=this.value"></div>
    <div class="row"><label>Min gap to cut<small>seconds</small></label>
      <input type="range" id="mingap" min="0.1" max="2" step="0.05" value="0.30" oninput="document.getElementById('n-mingap').value=this.value">
      <input type="number" id="n-mingap" value="0.30" step="0.05" oninput="document.getElementById('mingap').value=this.value"></div>
    <div class="row"><label>Replace gap with<small>seconds</small></label>
      <input type="range" id="target" min="0" max="1" step="0.05" value="0.30" oninput="document.getElementById('n-target').value=this.value">
      <input type="number" id="n-target" value="0.30" step="0.05" oninput="document.getElementById('target').value=this.value"></div>
  </div>
  <div class="card">
    <h2>What to remove</h2>
    <div class="toggles">
      <div class="toggle on" data-key="cut_head"><div class="lbl">Cut head silence<small>Trim dead air at start</small></div><div class="switch"></div></div>
      <div class="row"><label>Head buffer<small>seconds of pause kept before speech starts</small></label>
        <input type="range" id="headbuf" min="0" max="2" step="0.1" value="0.5" oninput="document.getElementById('n-headbuf').value=this.value">
        <input type="number" id="n-headbuf" value="0.5" step="0.1" min="0" oninput="document.getElementById('headbuf').value=this.value"></div>
      <div class="toggle on" data-key="cut_tail"><div class="lbl">Cut tail silence<small>Trim dead air at end</small></div><div class="switch"></div></div>
      <div class="row"><label>Tail buffer<small>seconds of pause kept after speech ends</small></label>
        <input type="range" id="tailbuf" min="0" max="2" step="0.1" value="0.5" oninput="document.getElementById('n-tailbuf').value=this.value">
        <input type="number" id="n-tailbuf" value="0.5" step="0.1" min="0" oninput="document.getElementById('tailbuf').value=this.value"></div>
      <div class="toggle on" data-key="remove_retakes"><div class="lbl">Remove retakes<small>Detect repeated phrases, keep last take</small></div><div class="switch"></div></div>
      <div class="toggle" data-key="remove_clicks"><div class="lbl">Remove clicks &amp; pops<small>Short transients between silences</small></div><div class="switch"></div></div>
      <div class="toggle" data-key="stabilize"><div class="lbl">Stabilize video<small>Smooth handheld camera shake (adds render time)</small></div><div class="switch"></div></div>
      <div class="row"><label>Camera movement<small>CapCut-style synthetic motion on static footage. Dynamic follows the speaker's face.</small></label>
        <select id="camera_movement" style="flex:1;background:#1E2434;border:1px solid #343E5B;color:#E9ECED;padding:7px 9px;border-radius:5px;font-size:12px">
          <option value="none">None</option>
          <option value="zoom">Zoom — slow zoom in</option>
          <option value="shake">Shake — simulated handheld</option>
          <option value="soft">Soft — gentle ambient drift</option>
          <option value="dynamic">Dynamic — follows the speaker's face</option>
        </select></div>
    </div>
  </div>
  <div class="card">
    <h2>Audio enhancement <span style="color:#7a8497;font-size:9px;font-weight:400;text-transform:none;letter-spacing:0;margin-left:8px">volume &amp; enhance update the preview live, no render needed &middot; denoise needs a quick reprocess</span></h2>
    <div class="row"><label>Volume<small>% of original, live preview</small></label>
      <input type="range" id="volume" min="0" max="200" step="5" value="100" oninput="document.getElementById('n-volume').value=this.value; onAudioParamInput()">
      <input type="number" id="n-volume" value="100" min="0" max="200" step="5" oninput="document.getElementById('volume').value=this.value; onAudioParamInput()"></div>
    <div class="row"><label>Background noise removal<small>RNNoise + spectral denoise. 0 = off</small></label>
      <input type="range" id="denoise" min="0" max="1" step="0.05" value="0" oninput="document.getElementById('n-denoise').value=this.value; onAudioParamInput()">
      <input type="number" id="n-denoise" value="0" min="0" max="1" step="0.05" oninput="document.getElementById('denoise').value=this.value; onAudioParamInput()"></div>
    <div class="row"><label>Studio sound enhance<small>EQ + compress + loudness norm. 0 = off</small></label>
      <input type="range" id="enhance" min="0" max="1" step="0.05" value="0" oninput="document.getElementById('n-enhance').value=this.value; onAudioParamInput()">
      <input type="number" id="n-enhance" value="0" min="0" max="1" step="0.05" oninput="document.getElementById('enhance').value=this.value; onAudioParamInput()"></div>
  </div>
  <div class="card hidden" id="wf-card">
    <h2>Waveform <span style="color:#7a8497;font-size:9px;font-weight:400;text-transform:none;letter-spacing:0;margin-left:8px">red = silences cut</span></h2>
    <canvas id="wf" style="width:100%;height:120px;background:#0F121A;border-radius:6px;display:block"></canvas>
  </div>
  <div class="card hidden" id="tr-card">
    <h2>Transcript editor <span style="color:#7a8497;font-size:9px;font-weight:400;text-transform:none;letter-spacing:0;margin-left:8px">click word to delete &middot; click again to restore &middot; double-click to fix wrong text</span></h2>
    <div id="tr-words" style="line-height:2.2;font-size:13px;max-height:240px;overflow-y:auto;padding:8px;background:#0F121A;border-radius:6px"></div>
    <div id="tr-cuts" style="margin-top:10px;font-size:11px;color:#7a8497"></div>
  </div>
  <div class="actrow">
    <button class="btn" id="b-clean" onclick="runClean()">Clean video</button>
    <button class="btn secondary" id="b-undo" onclick="undoClean()" disabled>↶ Undo last</button>
  </div>
  <div id="clean-result" class="hidden"></div>
</div>

<div id="t-edit" class="wrap hidden">
  <div class="card">
    <h2>Source for editing</h2>
    <div id="edit-src" class="status">Complete Phase 1 first OR paste a clean video path:
      <input type="text" id="edit-path" placeholder="C:\Users\...\video.clean.mp4" style="width:100%;background:#1E2434;border:1px solid #343E5B;color:#E9ECED;padding:7px 9px;border-radius:5px;font-family:monospace;font-size:10px;margin-top:8px">
    </div>
  </div>
  <div class="card">
    <h2>Plan</h2>
    <div class="toolrow">
      <button class="primary" id="b-edit" onclick="runEdit()">Auto-edit</button>
      <select id="tmpl-sel"><option value="">— or pick template —</option></select>
      <button onclick="applyTemplate()">Apply template</button>
      <span style="flex:1"></span>
      <button onclick="planUndo()">↶ Undo</button>
      <button onclick="planRedo()">↷ Redo</button>
      <select id="aspect-sel" title="Output orientation (applies on next render)" onchange="setAspect()">
        <option value="auto">Orientation: match footage</option>
        <option value="16:9">Orientation: force 16:9 (pillarbox)</option>
      </select>
      <button onclick="loadPlan()" title="Pull in edits Claude applied from the prompt queue">⟳ Load Claude's edits</button>
      <button class="primary" onclick="rerender()">Re-render preview</button>
    </div>
    <div class="timeline" id="timeline"><div class="tl-ruler" id="tl-ruler">0s</div></div>
    <div class="toolrow" style="margin-top:8px">
      <select id="add-kind">
        <option value="hook_title">hook_title</option>
        <option value="word_pop" selected>word_pop</option>
        <option value="stat_punch">stat_punch</option>
        <option value="quote_pull">quote_pull</option>
        <option value="image_card">image_card</option>
        <option value="tool_logo_burst">tool_logo_burst</option>
        <option value="bar_overlay">bar_overlay</option>
        <option value="bullet_burst">bullet_burst</option>
        <option value="ratio_dots">ratio_dots</option>
        <option value="subscribe">subscribe</option>
        <option value="ring_chart">ring_chart (pie/donut)</option>
        <option value="countdown_reveal">countdown_reveal</option>
        <option value="logo_reveal_hero">logo_reveal_hero</option>
        <option value="logo_reveal_style">logo_reveal_style</option>
        <option value="image_compare_slider">image_compare_slider</option>
        <option value="end_card">end_card</option>
        <option value="gallery_grid">gallery_grid</option>
        <option value="image_carousel">image_carousel</option>
        <option value="image_zoom_reveal">image_zoom_reveal</option>
        <option value="masonry_gallery">masonry_gallery</option>
        <option value="photo_stack">photo_stack</option>
        <option value="picture_in_picture">picture_in_picture</option>
        <option value="polaroid_frame">polaroid_frame</option>
        <option value="split_panels">split_panels</option>
        <option value="area_chart">area_chart (gradient trend line)</option>
        <option value="progress_bars">progress_bars (skill/metric bars)</option>
        <option value="stat_delta">stat_delta (count-up + delta chip)</option>
        <option value="comparison_bars">comparison_bars (before/after rows)</option>
        <option value="circular_progress">circular_progress (ring meter)</option>
        <option value="bounce_title">bounce_title (spring bounce title)</option>
        <option value="bubble_pop_text">bubble_pop_text (chars in bubbles)</option>
        <option value="pop_text">pop_text (neon scale-pop text)</option>
        <option value="pulse_text">pulse_text (breathing glow text)</option>
        <option value="text_sweep">text_sweep (word highlight sweep)</option>
        <option value="typewriter_text">typewriter_text (typing + cursor)</option>
        <option value="list_reveal">list_reveal (compact stagger list)</option>
        <option value="card_flip">card_flip (3D card flip)</option>
        <option value="notification_stack">notification_stack (stacked toasts)</option>
        <option value="carousel_3d">carousel_3d (orbiting 3D cards)</option>
        <option value="sound_wave">sound_wave (audio waveform bars)</option>
      </select>
      <button onclick="addBeat()">+ Add beat</button>
      <span style="color:#7a8497;font-size:11px">Drag beats to move · drag edges to resize · click to edit</span>
    </div>
    <div id="beat-editor"></div>
  </div>
  <div class="card">
    <h2>Music bed</h2>
    <div class="music-list" id="music-list"></div>
  </div>
  <div class="card">
    <h2>Chat — refine plan</h2>
    <div class="chat" id="chat"><div class="msg assistant">Use the toolbar above to edit the plan. Or type feedback here to add a note.</div></div>
    <div class="chatform">
      <input type="text" id="chatmsg" placeholder="e.g. add a bar chart at 50%..." onkeydown="if(event.key==='Enter')sendChat()">
      <button onclick="sendChat()">Send</button>
    </div>
  </div>
  <div class="actrow">
    <button class="btn secondary" onclick="openTools()">Open Remotion Studio + Tuner</button>
  </div>
  <div id="edit-result" class="hidden"></div>
</div>

<div class="modal" id="modal-stock">
  <div class="modal-body">
    <h3>Search Pexels stock</h3>
    <div class="toolrow"><input type="text" id="stock-q" placeholder="search..." style="flex:1"><button class="primary" onclick="stockSearch()">Search</button><button onclick="closeModal('modal-stock')">×</button></div>
    <div class="thumbs" id="stock-thumbs"></div>
  </div>
</div>
<div class="modal" id="modal-errors">
  <div class="modal-body">
    <h3>Error log <button onclick="closeModal('modal-errors')" style="float:right;background:none;border:none;color:var(--muted);font-size:16px;cursor:pointer">×</button></h3>
    <div id="err-list" style="font-family:monospace;font-size:11px;max-height:60vh;overflow-y:auto"></div>
  </div>
</div>
<div class="modal" id="modal-tour">
  <div class="modal-body">
    <h3>Welcome to Video Studio <button onclick="closeModal('modal-tour')" style="float:right;background:none;border:none;color:var(--muted);font-size:16px;cursor:pointer">×</button></h3>
    <div style="font-size:13px;line-height:1.7;color:var(--muted)">
      <p style="margin-bottom:10px"><strong style="color:var(--accent)">3 tabs, left to right:</strong></p>
      <p><strong>1. Clean</strong> — drop raw video, adjust silence/gap/retake removal, click word in transcript to delete it, watch the waveform, toggle audio enhancement intensity. Outputs <code>clean.mp4</code>.</p>
      <p><strong>2. Edit</strong> — auto-generate a beat plan or pick a template. Drag/resize beats on the timeline. Click a beat to edit every field. Fetch logos, search Pexels stock, pick music. Re-render preview.</p>
      <p><strong>3. Export</strong> — pick quality preset → render final 1080p w/ music + SFX → download.</p>
      <p style="margin-top:14px"><strong style="color:var(--accent)">Keyboard:</strong></p>
      <ul style="margin-left:18px;font-size:12px">
        <li><code>Space</code> — play/pause preview video</li>
        <li><code>J / K / L</code> — scrub backward / pause / forward (1.0×, 1.5×, 2.0×)</li>
        <li><code>Ctrl+Z / Ctrl+Y</code> — undo / redo plan edits</li>
        <li><code>Ctrl+S</code> — save project (resumable from Projects tab)</li>
        <li><code>1 / 2 / 3</code> — switch tabs</li>
      </ul>
      <p style="margin-top:14px"><strong style="color:var(--accent)">Tools:</strong></p>
      <ul style="margin-left:18px;font-size:12px">
        <li><strong>Remotion Studio (:5057)</strong> — live preview of rendered comp w/ HMR on template edits</li>
        <li><strong>Position Tuner (:5058)</strong> — slider-based placement tuning for every overlay</li>
      </ul>
    </div>
  </div>
</div>
<div class="modal" id="modal-logo">
  <div class="modal-body">
    <h3>Fetch brand logo</h3>
    <div class="toolrow"><input type="text" id="logo-q" placeholder="Stripe, Notion, Hugging Face..." style="flex:1"><button class="primary" onclick="logoFetch()">Fetch</button><button onclick="closeModal('modal-logo')">×</button></div>
    <div id="logo-result" style="margin-top:14px;font-size:12px;color:#B5BFC2"></div>
  </div>
</div>

<div id="t-export" class="wrap hidden">
  <div class="card">
    <h2>Quality preset</h2>
    <div class="preset" id="preset">
      <div data-p="draft">Draft<br><small>720p / 30fps</small></div>
      <div data-p="standard" class="sel">Standard<br><small>1080p / 30fps</small></div>
      <div data-p="high">High<br><small>1080p / 60fps</small></div>
      <div data-p="pro">Pro<br><small>4K / 30fps</small></div>
    </div>
  </div>
  <button class="btn" id="b-final" onclick="runFinal()">Render Final</button>
  <div id="export-result" class="hidden"></div>
</div>

</main>

<script>
const $ = id => document.getElementById(id);
let sid = null;
let preset = 'standard';
function setSid(newSid){
  sid = newSid;
  if(sid) localStorage.setItem('activeSid', sid);
  else localStorage.removeItem('activeSid');
}
// Resume the live session after an accidental reload (Ctrl+R, F5, etc).
// Only works while the server process hasn't restarted — SESSIONS is
// in-memory. If the server DID restart, /state 404s and we just clear the
// stale id and land on a blank Clean tab, same as before this existed.
(async function resumeSession(){
  const saved = localStorage.getItem('activeSid');
  if(!saved) return;
  try{
    const r = await fetch('/state?sid='+saved);
    if(!r.ok){ localStorage.removeItem('activeSid'); return; }
    const state = await r.json();
    if(state.error){ localStorage.removeItem('activeSid'); return; }
    setSid(saved);
    toast('Resumed your session');
    hydrateProject(state);
  } catch(e){ /* server not reachable yet — leave it, next reload will retry */ }
})();

function tab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelector('.tab[data-tab='+name+']').classList.add('active');
  ['dash','clean','edit','export'].forEach(n=>$('t-'+n).classList.add('hidden'));
  $('t-'+name).classList.remove('hidden');
  if(name==='dash') loadProjects();
}
// Browsers throttle setTimeout heavily in a backgrounded tab (sometimes to
// once every several seconds, occasionally further) -- during a multi-minute
// stabilize/camera-movement/splice render, alt-tabbing away and back made the
// visible progress bar look frozen even though the server had moved on and
// finished, confirmed directly by forcing a poll manually. Firing one poll
// immediately on refocus re-syncs the UI without waiting on the throttled timer.
document.addEventListener('visibilitychange', () => {
  if(document.hidden || !sid) return;
  if(!$('t-clean').classList.contains('hidden')) pollClean();
  if(!$('t-edit').classList.contains('hidden')) pollEdit();
  if(!$('t-export').classList.contains('hidden')) pollFinal();
});
function toggleTheme(){
  document.body.classList.toggle('light');
  localStorage.setItem('theme', document.body.classList.contains('light')?'light':'dark');
}
if(localStorage.getItem('theme')==='light') document.body.classList.add('light');
if(!localStorage.getItem('seen_tour')){ setTimeout(()=>openModal('modal-tour'), 600); localStorage.setItem('seen_tour','1'); }

async function saveProject(){
  if(!sid){ alert('Nothing to save yet. Clean a video first.'); return; }
  const r = await fetch('/project/save',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid})});
  const j = await r.json();
  if(j.path) toast('💾 Saved');
}
function toast(msg){
  const d = document.createElement('div');
  d.textContent = msg;
  d.style.cssText='position:fixed;bottom:20px;right:20px;background:var(--accent);color:var(--accent-dark);padding:10px 18px;border-radius:6px;font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:.05em;box-shadow:0 8px 24px rgba(0,0,0,0.4);z-index:200';
  document.body.appendChild(d);
  setTimeout(()=>d.remove(), 2200);
}
async function loadProjects(){
  const r = await fetch('/projects'); const j = await r.json();
  const grid = $('proj-grid'); grid.innerHTML='';
  if(!j.projects || !j.projects.length){ $('proj-empty').style.display='block'; return; }
  $('proj-empty').style.display='none';
  j.projects.forEach(p=>{
    const div = document.createElement('div');
    div.style.cssText='background:var(--input-bg);border:1px solid var(--border);border-radius:8px;padding:12px;cursor:pointer;transition:.15s';
    div.onmouseenter = ()=>div.style.borderColor='var(--accent)';
    div.onmouseleave = ()=>div.style.borderColor='var(--border)';
    div.innerHTML = `${p.thumb?`<img src="/thumb?p=${encodeURIComponent(p.thumb)}" style="width:100%;height:120px;object-fit:cover;border-radius:5px;margin-bottom:8px">`:'<div style="width:100%;height:120px;background:#000;border-radius:5px;margin-bottom:8px;display:flex;align-items:center;justify-content:center;font-size:30px">🎬</div>'}
      <div style="font-size:13px;color:var(--text);font-weight:700">${esc(p.name)}</div>
      <div style="font-size:11px;color:var(--muted);text-overflow:ellipsis;overflow:hidden;white-space:nowrap;margin-top:2px" title="${esc(p.video_name)}">${esc(p.video_name)}</div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px">
        <div style="font-size:10px;color:var(--dim)">${new Date(p.saved_at*1000).toLocaleString()} · ${p.status}</div>
        <button onclick="deleteProject(event,'${p.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'")}')" style="background:none;border:1px solid #5b2a2a;color:#ffb3b3;padding:2px 8px;border-radius:4px;font-size:10px;cursor:pointer">Delete</button>
      </div>`;
    div.onclick = ()=>openProject(p.path);
    grid.appendChild(div);
  });
}
async function openProject(path){
  const r = await fetch('/project/open',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({path})});
  const j = await r.json();
  if(!j.sid){ alert(j.error||'Failed to load project'); return; }
  setSid(j.sid);
  const sr = await fetch('/state?sid='+sid); const state = await sr.json();
  toast('Project loaded');
  hydrateProject(state);
}
async function deleteProject(ev, path){
  ev.stopPropagation();
  if(!confirm('Delete this saved project? The video file itself is not touched.')) return;
  await fetch('/project/delete',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({path})});
  loadProjects();
}
function hydrateProject(j){
  // knobs
  const setRange = (id, val) => { if(val==null) return; $(id).value=val; $('n-'+id).value=val; };
  setRange('noise', j.noise_db); setRange('mingap', j.min_gap); setRange('target', j.target_gap);
  setRange('denoise', j.denoise_intensity); setRange('enhance', j.enhance_intensity);
  setRange('volume', j.volume_pct);
  setRange('headbuf', j.head_buffer_sec); setRange('tailbuf', j.tail_buffer_sec);
  if(j.camera_movement) $('camera_movement').value = j.camera_movement;
  ['cut_head','cut_tail','remove_retakes','remove_clicks','stabilize'].forEach(k=>{
    const el = document.querySelector('.toggle[data-key='+k+']');
    if(el) el.classList.toggle('on', !!j[k]);
  });
  clipQueue = (j.sources && j.sources.length) ? j.sources.slice() : (j.src ? [j.src] : []);
  renderClips();
  if(j.aspect) $('aspect-sel').value = j.aspect;
  // clean tab
  renderClean(j);
  if(j.clean_path){ $('edit-path').value = j.clean_path; loadWaveform(); loadTranscript(); }
  // edit tab
  if(j.chat) renderChat(j.chat);
  renderEdit(j);
  loadPlan(); loadMusic();
  // export tab
  if(j.preset){ preset = j.preset; document.querySelectorAll('.preset div').forEach(d=>d.classList.toggle('sel', d.dataset.p===preset)); }
  if(j.final_path){
    $('export-result').classList.remove('hidden');
    $('export-result').innerHTML = `<div class="card"><h2>🎬 Final ready</h2>
      <video class="player" controls src="/file?p=${encodeURIComponent(j.final_path)}&t=${Date.now()}"></video>
      <a class="dl" href="/file?p=${encodeURIComponent(j.final_path)}&dl=1" download>Download final .mp4</a></div>`;
  }
  // land on whichever tab matches how far the project got
  const st = j.status || '';
  if(st.startsWith('export')) tab('export');
  else if(st.startsWith('edit')) tab('edit');
  else tab('clean');
}
async function pollErrors(){
  try{ const r = await fetch('/errors'); const j = await r.json();
    $('err-count').textContent = (j.errors||[]).length;
    const list = $('err-list'); list.innerHTML = '';
    (j.errors||[]).slice().reverse().forEach(e=>{
      const d = document.createElement('div');
      d.style.cssText='padding:8px 10px;background:var(--input-bg);border-left:3px solid #ef4444;border-radius:4px;margin-bottom:6px';
      const dt = new Date(e.ts*1000).toLocaleTimeString();
      d.innerHTML = `<div style="font-size:9px;color:var(--dim)">${dt} · ${e.where} · ${e.sid?.slice(0,8)||''}</div><div style="color:#ffb3b3">${esc(e.msg)}</div>`;
      list.appendChild(d);
    });
  } catch(e){}
}
setInterval(pollErrors, 5000); pollErrors();
setInterval(()=>{ if(sid) saveProjectQuiet(); }, 30000);
async function saveProjectQuiet(){ if(!sid) return; await fetch('/project/save',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid})}); }

// Global keyboard shortcuts
let scrubRate = 1.0;
document.addEventListener('keydown', e=>{
  if(e.target.tagName==='INPUT' || e.target.tagName==='TEXTAREA') return;
  const vids = document.querySelectorAll('video');
  const v = Array.from(vids).find(x=>x.offsetParent !== null);  // visible one
  if(e.key===' '){ if(v){ e.preventDefault(); v.paused?v.play():v.pause(); } return; }
  if(e.key==='j' || e.key==='J'){ if(v){ e.preventDefault(); v.currentTime = Math.max(0, v.currentTime - 5); } return; }
  if(e.key==='k' || e.key==='K'){ if(v){ e.preventDefault(); v.pause(); } return; }
  if(e.key==='l' || e.key==='L'){ if(v){ e.preventDefault(); v.currentTime = Math.min(v.duration, v.currentTime + 5); v.play(); } return; }
  if((e.ctrlKey||e.metaKey) && e.key==='s'){ e.preventDefault(); saveProject(); return; }
  if(e.key==='1'){ tab('clean'); } if(e.key==='2'){ tab('edit'); } if(e.key==='3'){ tab('export'); }
});

const drop=$('drop'),file=$('file'),pathInput=$('path');
let clipQueue = [];
function renderClips(){
  const div = $('clip-list'); div.innerHTML = '';
  clipQueue.forEach((p, i)=>{
    const row = document.createElement('div');
    row.draggable = true; row.dataset.idx = i;
    row.style.cssText = 'display:grid;grid-template-columns:24px 1fr auto auto;gap:8px;align-items:center;background:var(--input-bg);border:1px solid var(--input-border);border-radius:6px;padding:8px 10px;font-size:11px;cursor:move';
    row.innerHTML = `<div style="color:var(--dim);font-family:monospace">${i+1}</div><div style="font-family:monospace;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(p)}">${esc(p.split(/[\\/]/).pop())}</div>
      <button onclick="moveClip(${i},-1)" ${i===0?'disabled':''} style="background:transparent;border:1px solid var(--input-border);color:var(--muted);padding:2px 8px;border-radius:3px;cursor:pointer;font-size:11px">↑</button>
      <button onclick="moveClip(${i},1)" ${i===clipQueue.length-1?'disabled':''} style="background:transparent;border:1px solid var(--input-border);color:var(--muted);padding:2px 8px;border-radius:3px;cursor:pointer;font-size:11px">↓</button>
      <button onclick="removeClip(${i})" style="background:transparent;border:1px solid #5b2a2a;color:#ffb3b3;padding:2px 8px;border-radius:3px;cursor:pointer;font-size:11px">×</button>`;
    div.appendChild(row);
  });
  if(clipQueue.length === 1) pathInput.value = clipQueue[0];
  else if(clipQueue.length === 0) pathInput.value = '';
  else pathInput.value = `(${clipQueue.length} clips queued — combine + clean)`;
}
function addClip(p){ if(p && !clipQueue.includes(p)) clipQueue.push(p); renderClips(); }
function moveClip(i, dir){
  const j = i + dir;
  if(j < 0 || j >= clipQueue.length) return;
  [clipQueue[i], clipQueue[j]] = [clipQueue[j], clipQueue[i]];
  renderClips();
}
function removeClip(i){ clipQueue.splice(i,1); renderClips(); }

drop.addEventListener('click', e=>{ if(e.target.tagName!=='INPUT' && e.target.tagName!=='BUTTON') file.click(); });
drop.addEventListener('dragover', e=>{ e.preventDefault(); drop.style.borderColor='#CFFF05'; });
drop.addEventListener('drop', e=>{ e.preventDefault(); drop.style.borderColor='';
  Array.from(e.dataTransfer.files).forEach(f=>addClip(f.path || f.name));
});
file.addEventListener('change', e=>{
  Array.from(e.target.files).forEach(f=>addClip(f.path || f.name));
});
pathInput.addEventListener('change', e=>{
  // if user types a path manually and queue is empty, use it as the only clip
  if(clipQueue.length===0 && e.target.value.trim()) addClip(e.target.value.trim());
});
document.querySelectorAll('.toggle').forEach(t=>t.addEventListener('click', ()=>t.classList.toggle('on')));
document.querySelectorAll('.preset div').forEach(d=>d.addEventListener('click', ()=>{
  document.querySelectorAll('.preset div').forEach(x=>x.classList.remove('sel'));
  d.classList.add('sel'); preset=d.dataset.p;
}));

// ── Live audio preview (Web Audio API) ──────────────────────────────────
// Volume and "enhance" (EQ + compressor) both have native real-time Web
// Audio equivalents, so those can update the PLAYING preview instantly —
// zero server round-trip, zero render — matching how CapCut's sliders
// work. Denoise (afftdn, spectral noise removal) has no native browser
// primitive; it still needs a server-side reprocess pass (the cheap
// audio-only one from build_audio_chain/apply_audio_filter), which now
// only fires once, lazily, right before download or moving to Edit —
// not on every slider tick.
let audioCtx = null;
function attachLiveAudio(videoEl){
  if(!videoEl || videoEl._audioGraph) return videoEl && videoEl._audioGraph;
  if(!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  try{
    const source = audioCtx.createMediaElementSource(videoEl);
    const highpass = audioCtx.createBiquadFilter(); highpass.type = 'highpass'; highpass.frequency.value = 20;
    const presence = audioCtx.createBiquadFilter(); presence.type = 'peaking'; presence.frequency.value = 4000; presence.Q.value = 1; presence.gain.value = 0;
    const compressor = audioCtx.createDynamicsCompressor(); compressor.threshold.value = -100; compressor.ratio.value = 1;
    const gain = audioCtx.createGain(); gain.gain.value = (+$('volume').value || 100) / 100;
    source.connect(highpass); highpass.connect(presence); presence.connect(compressor); compressor.connect(gain); gain.connect(audioCtx.destination);
    videoEl._audioGraph = { highpass, presence, compressor, gain };
    updateLiveAudio(videoEl);
  }catch(e){ /* e.g. element already has a source elsewhere — ignore, native audio still plays */ }
  return videoEl._audioGraph;
}
function updateLiveAudio(videoEl){
  const g = videoEl && videoEl._audioGraph; if(!g) return;
  if(audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
  const enhanceI = +($('enhance') && $('enhance').value || 0);
  const volumePct = +($('volume') && $('volume').value || 100);
  g.highpass.frequency.setTargetAtTime(enhanceI > 0.02 ? (60 + 60 * enhanceI) : 20, audioCtx.currentTime, 0.02);
  g.presence.gain.setTargetAtTime(enhanceI > 0.02 ? (2 + 3 * enhanceI) : 0, audioCtx.currentTime, 0.02);
  g.compressor.threshold.setTargetAtTime(enhanceI > 0.02 ? -18 : -100, audioCtx.currentTime, 0.02);
  g.compressor.ratio.setTargetAtTime(enhanceI > 0.02 ? (2 + 2 * enhanceI) : 1, audioCtx.currentTime, 0.02);
  g.gain.gain.setTargetAtTime(volumePct / 100, audioCtx.currentTime, 0.02);
}
function liveAudioTargets(){
  // Every currently-mounted preview <video> — Clean-tab result and Edit-tab
  // preview both use class="player", and either/both may be in the DOM.
  return Array.from(document.querySelectorAll('video.player'));
}
function onAudioParamInput(){
  liveAudioTargets().forEach(v=>{ attachLiveAudio(v); updateLiveAudio(v); });
  pendingAudioBake = true; // only actually re-render once, lazily, before download/edit
  if(sid){
    clearTimeout(audioSaveDebounce);
    audioSaveDebounce = setTimeout(()=>{
      fetch('/clean/set_audio_params', {method:'POST', headers:{'content-type':'application/json'},
        body:JSON.stringify({sid, denoise_intensity:+$('denoise').value, enhance_intensity:+$('enhance').value, volume_pct:+$('volume').value})});
    }, 400);
  }
}
let audioSaveDebounce = null;
let pendingAudioBake = false;
async function ensureAudioBaked(){
  // Called right before the user actually needs the real file (download or
  // move to Edit) — this is the one point where the cheap audio-only
  // reprocess actually runs, instead of on every slider tick.
  if(!pendingAudioBake || !sid) return;
  pendingAudioBake = false;
  await fetch('/clean/bake_audio', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid})});
}

function gatherCleanParams(){
  let sources = clipQueue.length ? clipQueue.slice() : [];
  if(!sources.length){
    const p = pathInput.value.trim();
    if(p && !p.startsWith('(')) sources = [p];
  }
  const params = { sources, src: sources[0]||'', noise_db:+$('noise').value, min_gap:+$('mingap').value, target_gap:+$('target').value,
    denoise_intensity:+$('denoise').value, enhance_intensity:+$('enhance').value, volume_pct:+$('volume').value,
    head_buffer_sec:+$('headbuf').value, tail_buffer_sec:+$('tailbuf').value,
    camera_movement:$('camera_movement').value };
  ['cut_head','cut_tail','remove_retakes','remove_clicks','stabilize'].forEach(k=>{
    params[k] = document.querySelector('.toggle[data-key='+k+']').classList.contains('on');
  });
  return params;
}
let awaitingApproval = false;
async function runClean(){
  if(awaitingApproval && sid){
    // User has reviewed/corrected the transcript and is ready for the
    // actual cut+render to happen.
    $('b-clean').disabled=true; $('b-clean').textContent='Processing...';
    awaitingApproval = false;
    await fetch('/clean/approve', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid})});
    pollClean(); return;
  }
  const params = gatherCleanParams();
  if(!params.sources.length){ alert('Drop at least one video'); return; }
  $('b-clean').disabled=true; $('b-clean').textContent='Processing...';
  if(sid){
    // re-clean with new knobs (preserve manual_cuts) — transcript's
    // already been approved once, so this goes straight to analyze/splice.
    await fetch('/clean/reclean', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid, ...params})});
    pollClean(); return;
  }
  const r = await fetch('/clean/start', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(params)});
  const j = await r.json();
  if(j.error){ showCleanError(j.error); return; }
  setSid(j.sid); pollClean();
  setTimeout(loadWaveform, 800);
  setTimeout(loadTranscript, 1500);
}
async function undoClean(){
  if(!sid) return;
  $('b-undo').disabled=true;
  const r = await fetch('/clean/undo', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid})});
  const j = await r.json();
  if(j.error){ alert(j.error); $('b-undo').disabled=false; return; }
  pollClean();
}
async function loadWaveform(){
  if(!sid) return;
  const r = await fetch('/waveform?sid='+sid); const j = await r.json();
  if(!j.peaks || !j.peaks.length) return;
  $('wf-card').classList.remove('hidden');
  const c = $('wf'); const ctx = c.getContext('2d');
  c.width = c.clientWidth; c.height = 120;
  ctx.fillStyle = '#0F121A'; ctx.fillRect(0,0,c.width,c.height);
  // silence bands
  const dur = j.duration;
  (j.silences||[]).forEach(s=>{
    const x = (s[0]/dur) * c.width;
    const w = ((s[1]-s[0])/dur) * c.width;
    ctx.fillStyle = 'rgba(255,107,107,0.18)'; ctx.fillRect(x,0,w,c.height);
  });
  // peaks
  ctx.fillStyle = '#CFFF05';
  const w = c.width / j.peaks.length;
  j.peaks.forEach((p, i)=>{
    const h = Math.max(2, p * c.height * 0.9);
    ctx.fillRect(i*w, (c.height-h)/2, Math.max(1, w-0.5), h);
  });
}
async function loadTranscript(){
  if(!sid) return;
  const r = await fetch('/transcript?sid='+sid); const j = await r.json();
  if(!j.words || !j.words.length) return;
  $('tr-card').classList.remove('hidden');
  const div = $('tr-words'); div.innerHTML = '';
  const cuts = j.manual_cuts || [];
  const isCut = (w) => cuts.some(c => c[0] <= w.start && w.end <= c[1]);
  j.words.forEach((w, i)=>{
    const span = document.createElement('span');
    span.textContent = w.word + ' ';
    span.dataset.start = w.start; span.dataset.end = w.end;
    span.style.cursor = 'pointer'; span.style.padding = '2px 4px'; span.style.borderRadius = '3px';
    if(isCut(w)){ span.style.textDecoration = 'line-through'; span.style.color = '#5B6478'; span.style.background = 'rgba(255,107,107,0.1)'; }
    else { span.style.color = '#E9ECED'; }
    span.addEventListener('mouseenter', ()=>{ if(!isCut(w)) span.style.background='rgba(207,255,5,0.15)'; });
    span.addEventListener('mouseleave', ()=>{ if(!isCut(w)) span.style.background=''; });
    // Don't rely on the browser's native 'dblclick' event — its timing
    // window depends on OS mouse settings and is unreliable on trackpads,
    // where two deliberate clicks often land further apart than dblclick's
    // threshold, so it silently never fires and every click just re-runs
    // the single-click cut/restore toggle instead (exactly what "double-
    // clicking again and again for the sake of it" looks like). Measuring
    // the gap between successive 'click' events ourselves, with a longer
    // 450ms window, is the reliable version of the same idea.
    let clickTimer = null;
    let lastClickAt = 0;
    span.addEventListener('click', ()=>{
      const now = Date.now();
      const isDoubleClick = (now - lastClickAt) < 450;
      lastClickAt = now;
      if(isDoubleClick){
        if(clickTimer){ clearTimeout(clickTimer); clickTimer = null; }
        lastClickAt = 0;
        openWordEditor();
        return;
      }
      if(clickTimer) clearTimeout(clickTimer);
      clickTimer = setTimeout(async ()=>{
        clickTimer = null;
        if(isCut(w)){
          const idx = cuts.findIndex(c => c[0] <= w.start && w.end <= c[1]);
          await fetch('/transcript/restore', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid, idx})});
        } else {
          await fetch('/transcript/cut', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid, word_idx:i})});
        }
        loadTranscript();
      }, 450);
    });
    function openWordEditor(){
      const input = document.createElement('input');
      input.type = 'text';
      input.value = w.word;
      input.style.width = Math.max(50, w.word.length * 9) + 'px';
      input.style.background = '#1E2434'; input.style.border = '1px solid #CFFF05';
      input.style.color = '#E9ECED'; input.style.fontSize = '13px';
      input.style.borderRadius = '3px'; input.style.padding = '1px 4px';
      span.replaceWith(input);
      input.focus(); input.select();
      let saved = false;
      const save = async ()=>{
        if(saved) return; saved = true;
        const val = input.value.trim();
        if(val && val !== w.word){
          await fetch('/transcript/edit_word', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid, idx:i, text: val})});
        }
        loadTranscript();
      };
      input.addEventListener('keydown', (ev)=>{
        if(ev.key === 'Enter'){ input.blur(); }
        if(ev.key === 'Escape'){ saved = true; loadTranscript(); }
      });
      input.addEventListener('blur', save);
    }
    div.appendChild(span);
  });
  $('tr-cuts').textContent = cuts.length ? `${cuts.length} manual cut(s) — click Clean video to apply` : 'No manual cuts. Click any word above to mark for removal.';
}
// Shared /state fetch for all pollers below. A stale sid (server restarted
// mid-session — SESSIONS is in-memory, wiped on every restart) 404s here;
// without this guard each poller rendered `j.status` as literal "undefined"
// AND kept polling forever since none of their done/error checks ever
// matched, showing "⚡ undefined" on a permanent loop until reload.
async function fetchState(){
  const r = await fetch('/state?sid='+sid);
  const j = await r.json();
  if(!r.ok || j.error){
    toast('⚠ Session lost (server restarted) — reload the page');
    localStorage.removeItem('activeSid');
    return null;
  }
  return j;
}
async function pollClean(){
  const j = await fetchState(); if(!j) return;
  renderClean(j);
  if(j.status==='clean:awaiting_approval'){
    awaitingApproval = true;
    $('b-clean').disabled=false; $('b-clean').textContent='Approve transcript & Clean';
    loadWaveform(); loadTranscript();
    return; // job_clean has stopped here on purpose — wait for the user
  }
  if(j.status==='clean:done' || j.status==='clean:error'){
    awaitingApproval = false;
    $('b-clean').disabled=false; $('b-clean').textContent='Re-clean';
    $('b-undo').disabled = !(j.history && j.history.length > 1);
    if(j.status==='clean:done' && j.clean_path){
      $('edit-path').value = j.clean_path;
      loadWaveform(); loadTranscript();
    }
    return;
  }
  setTimeout(pollClean, 1200);
}
function renderClean(j){
  const div = $('clean-result'); div.classList.remove('hidden');
  if(j.status==='clean:done'){
    const saved = (j.clean_original - j.clean_new).toFixed(2);
    div.innerHTML = `<div class="card"><h2>Done</h2>
      <div class="stats">
        <div class="stat"><div class="v">${j.clean_original.toFixed(1)}s</div><div class="l">Original</div></div>
        <div class="stat"><div class="v">${j.clean_new.toFixed(1)}s</div><div class="l">Clean</div></div>
        <div class="stat"><div class="v">-${saved}s</div><div class="l">Saved</div></div>
      </div>
      <div class="status">Silences: <span class="ok">${j.clean_silences}</span> · Retakes cut: <span class="ok">${j.clean_retakes}</span> · Clicks: <span class="ok">${j.clean_clicks||0}</span> · Manual cuts: <span class="ok">${j.clean_manual||0}</span></div>
      <video class="player" controls src="/file?p=${encodeURIComponent(j.clean_path)}&t=${Date.now()}"></video>
      <button class="dl" style="margin-top:10px" onclick="downloadClean('${j.clean_path.replace(/\\/g,'\\\\').replace(/'/g,"\\'")}')">Download clean.mp4</button>
      ${(j.stabilize || (j.camera_movement && j.camera_movement !== 'none')) ?
        `<button class="btn secondary" style="margin-top:10px" onclick="applyCameraEffects()">🎥 Apply Camera Effects (stabilize/movement)</button>` : ''}
      <button class="btn secondary" style="margin-top:10px" onclick="continueToEdit()">Continue to Edit →</button></div>`;
    attachLiveAudio(div.querySelector('video.player'));
  } else if(j.status==='clean:error'){
    div.innerHTML = `<div class="card"><div class="status"><span class="err">ERROR</span> ${j.error||''}</div></div>`;
  } else if(j.status==='clean:awaiting_approval'){
    div.innerHTML = `<div class="card"><div class="status"><span class="ok">Transcript ready — review it below</span></div>
      <p style="color:#7a8497;font-size:12px;margin-top:6px">Double-click any wrong word to fix it before the video gets cut. Nothing is rendered yet — click "Approve transcript &amp; Clean" below when it looks right.</p></div>`;
  } else if(j.status==='clean:combining'){
    div.innerHTML = `<div class="card"><h2>Combining ${(j.sources||[]).length} clips</h2>${progressBarHtml(j.render_progress)}</div>`;
  } else if(j.status==='clean:stabilizing'){
    div.innerHTML = `<div class="card"><h2>Stabilizing</h2>${progressBarHtml(j.render_progress)}</div>`;
  } else if(j.status==='clean:camera_movement'){
    div.innerHTML = `<div class="card"><h2>Applying camera movement</h2>${progressBarHtml(j.render_progress)}</div>`;
  } else if(j.status==='clean:splicing'){
    div.innerHTML = `<div class="card"><h2>Cutting &amp; splicing</h2>${progressBarHtml(j.render_progress)}</div>`;
  } else {
    const label = j.status==='clean:transcribing' ? `⚡ ${j.transcribe_phase || 'Transcribing...'}` : `⚡ ${j.status}`;
    div.innerHTML = `<div class="card"><div class="status"><span class="ok">${label}</span></div></div>`;
  }
}
function showCleanError(m){ $('clean-result').classList.remove('hidden'); $('clean-result').innerHTML=`<div class="card"><div class="status"><span class="err">${m}</span></div></div>`; $('b-clean').disabled=false; $('b-clean').textContent='Try again'; }
async function downloadClean(path){
  // Volume/enhance only exist live in the browser's Web Audio graph until
  // now — this is the ONE point (not every slider tick) where they
  // actually get baked into the real file, right before it's needed.
  await ensureAudioBaked();
  const a = document.createElement('a');
  a.href = '/file?p=' + encodeURIComponent(path) + '&dl=1&t=' + Date.now();
  document.body.appendChild(a); a.click(); a.remove();
}
async function applyCameraEffects(){
  if(!sid) return;
  const r = await fetch('/clean/apply_camera_effects', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid})});
  const j = await r.json();
  if(j.error){ alert(j.error); return; }
  pollClean();
}

async function continueToEdit(){
  await ensureAudioBaked();
  tab('edit');
}

async function runEdit(){
  const p = $('edit-path').value.trim(); if(!p){ alert('Need a clean video path'); return; }
  if(currentPlan.length && !confirm('Auto-edit regenerates the plan from scratch and discards the current beats (including anything Claude added from the prompt queue). Continue?')) return;
  $('b-edit').disabled=true; $('b-edit').textContent='Editing...';
  if(!sid){
    const r = await fetch('/clean/start', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({src:p, cut_head:false, cut_tail:false, remove_retakes:false, noise_db:-32, min_gap:99, target_gap:0.3})});
    const j = await r.json(); setSid(j.sid);
  }
  await fetch('/edit/start', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid, src:p})});
  pollEdit();
  setTimeout(loadPlan, 2000);
  loadMusic();
}

// ───────────── Plan editor ─────────────
let currentPlan = []; let selectedIdx = -1; let videoDuration = 30;
const KIND_COLORS = {
  hook_title:'#CFFF05', word_pop:'#7ad9ff', stat_punch:'#ff9a3c', quote_pull:'#c084fc',
  image_card:'#34d399', tool_logo_burst:'#fbbf24', bar_overlay:'#f472b6', bullet_burst:'#94e0a3',
  subscribe:'#ef4444', portrait_burst:'#a78bfa', ratio_dots:'#60a5fa',
  ring_chart:'#e879f9', countdown_reveal:'#facc15', logo_reveal_hero:'#5eead4', logo_reveal_style:'#2dd4bf',
  image_compare_slider:'#38bdf8', end_card:'#fb7185',
  gallery_grid:'#818cf8', image_carousel:'#f97316', image_zoom_reveal:'#22d3ee',
  masonry_gallery:'#c026d3', photo_stack:'#fda4af', picture_in_picture:'#4ade80',
  polaroid_frame:'#fcd34d', split_panels:'#93c5fd',
  area_chart:'#84cc16', progress_bars:'#eab308', stat_delta:'#fb923c', comparison_bars:'#f43f5e',
  circular_progress:'#06b6d4', bounce_title:'#a3e635', bubble_pop_text:'#d946ef', pop_text:'#f472b6',
  pulse_text:'#8b5cf6', text_sweep:'#facc15', typewriter_text:'#2dd4bf', list_reveal:'#38bdf8',
  card_flip:'#fbbf24', notification_stack:'#fb7185', carousel_3d:'#4ade80', sound_wave:'#22d3ee'
};

async function loadPlan(){
  if(!sid) return;
  const r = await fetch('/plan?sid='+sid); const j = await r.json();
  if(!j.plan) return;
  currentPlan = j.plan; videoDuration = j.duration || 30;
  // populate template dropdown once
  const ts = $('tmpl-sel');
  if(ts.options.length <= 1 && j.templates){
    j.templates.forEach(t=>{ const o=document.createElement('option'); o.value=t.id; o.textContent=t.name; ts.appendChild(o); });
  }
  renderTimeline();
  renderBeatEditor();
}

function renderTimeline(){
  const tl = $('timeline');
  // clear non-ruler children
  Array.from(tl.children).forEach(c=>{ if(c.id!=='tl-ruler') c.remove(); });
  // ruler ticks
  const ruler = $('tl-ruler'); ruler.innerHTML = '';
  const steps = Math.min(20, Math.ceil(videoDuration));
  for(let i=0; i<=steps; i++){
    const t = (videoDuration/steps)*i;
    const span = document.createElement('span');
    span.style.position='absolute'; span.style.left=(i/steps*100)+'%'; span.style.transform='translateX(-50%)';
    span.textContent = t.toFixed(0)+'s';
    ruler.appendChild(span);
  }
  currentPlan.forEach((b, i)=>{
    const div = document.createElement('div');
    div.className='tl-beat'+(i===selectedIdx?' sel':'');
    const s=+b.start_sec||0, e=+b.end_sec||0;
    div.style.left=(s/videoDuration*100)+'%';
    div.style.width=(Math.max(0.01,(e-s)/videoDuration)*100)+'%';
    div.style.background=KIND_COLORS[b.kind]||'#5B6478';
    div.textContent=b.kind+' '+s.toFixed(1)+'s';
    div.dataset.idx=i;
    div.addEventListener('click', e=>{ e.stopPropagation(); selectedIdx=i; renderTimeline(); renderBeatEditor(); });
    // drag handles
    ['l','r'].forEach(side=>{
      const h = document.createElement('div'); h.className='tl-handle '+side;
      h.addEventListener('mousedown', ev=>{ ev.stopPropagation(); startResize(i, side==='l'?'start':'end', ev); });
      div.appendChild(h);
    });
    div.addEventListener('mousedown', ev=>{
      if(ev.target.classList.contains('tl-handle')) return;
      startDrag(i, ev);
    });
    tl.appendChild(div);
  });
}

function pxToSec(px){
  const tl = $('timeline');
  return Math.max(0, Math.min(videoDuration, (px/tl.clientWidth)*videoDuration));
}

function startDrag(idx, downEv){
  const tl = $('timeline'); const rect = tl.getBoundingClientRect();
  const start0 = +currentPlan[idx].start_sec; const end0 = +currentPlan[idx].end_sec;
  const dur = end0 - start0; const offset = (downEv.clientX - rect.left) - (start0/videoDuration)*rect.width;
  function move(e){
    const x = e.clientX - rect.left - offset;
    let ns = Math.max(0, Math.min(videoDuration-dur, (x/rect.width)*videoDuration));
    currentPlan[idx].start_sec = +ns.toFixed(2);
    currentPlan[idx].end_sec = +(ns+dur).toFixed(2);
    renderTimeline();
  }
  function up(){
    document.removeEventListener('mousemove',move); document.removeEventListener('mouseup',up);
    fetch('/plan/move',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid,idx,start_sec:currentPlan[idx].start_sec})});
  }
  document.addEventListener('mousemove',move); document.addEventListener('mouseup',up);
}

function startResize(idx, edge, downEv){
  const tl = $('timeline'); const rect = tl.getBoundingClientRect();
  function move(e){
    const t = pxToSec(e.clientX - rect.left);
    const MIN_DUR = 0.1;
    if(edge==='start') currentPlan[idx].start_sec = Math.min(+t.toFixed(2), currentPlan[idx].end_sec - MIN_DUR);
    else currentPlan[idx].end_sec = Math.max(+t.toFixed(2), currentPlan[idx].start_sec + MIN_DUR);
    renderTimeline();
  }
  function up(){
    document.removeEventListener('mousemove',move); document.removeEventListener('mouseup',up);
    const t = edge==='start'?currentPlan[idx].start_sec:currentPlan[idx].end_sec;
    fetch('/plan/resize',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid,idx,edge,t})});
  }
  document.addEventListener('mousemove',move); document.addEventListener('mouseup',up);
}

function renderBeatEditor(){
  const div = $('beat-editor');
  if(selectedIdx < 0 || !currentPlan[selectedIdx]){ div.innerHTML=''; return; }
  const b = currentPlan[selectedIdx];
  const fields = beatFields(b);
  div.innerHTML = `<div class="beat-editor"><h3>${b.kind} @ ${(+b.start_sec).toFixed(2)}s</h3>
    ${fields.map(f=>`<div class="beat-row"><label>${f.label}</label>${f.html}</div>`).join('')}
    <div class="beat-actions">
      <button onclick="dupBeat()">Duplicate</button>
      <button onclick="openModal('modal-logo')">Fetch logo...</button>
      <button onclick="openModal('modal-stock')">Pexels stock...</button>
      <button onclick="uploadAsset()">Upload image</button>
      <button class="danger" onclick="delBeat()">Delete</button>
    </div></div>`;
  // wire inputs
  div.querySelectorAll('[data-field]').forEach(el=>{
    el.addEventListener('change', e=>updateField(e.target.dataset.field, e.target.value));
  });
}

function beatFields(b){
  const out = [
    {label:'start_sec', html:`<input type="number" step="0.05" data-field="start_sec" value="${b.start_sec}">`},
    {label:'end_sec', html:`<input type="number" step="0.05" data-field="end_sec" value="${b.end_sec}">`},
    {label:'kind', html:`<select data-field="kind">${['hook_title','word_pop','stat_punch','quote_pull','image_card','tool_logo_burst','bar_overlay','bullet_burst','ratio_dots','subscribe','portrait_burst','ring_chart','countdown_reveal','logo_reveal_hero','logo_reveal_style','image_compare_slider','end_card','gallery_grid','image_carousel','image_zoom_reveal','masonry_gallery','photo_stack','picture_in_picture','polaroid_frame','split_panels','area_chart','progress_bars','stat_delta','comparison_bars','circular_progress','bounce_title','bubble_pop_text','pop_text','pulse_text','text_sweep','typewriter_text','list_reveal','card_flip','notification_stack','carousel_3d','sound_wave'].map(k=>`<option ${b.kind===k?'selected':''} value="${k}">${k}</option>`).join('')}</select>`},
    {label:'fx (optional)', html:`<select data-field="fx"><option value="" ${!b.fx?'selected':''}>none</option>${['bokeh_circles','geometric_patterns','gradient_shift','grid_pulse','liquid_wave','matrix_rain','noise_grain','pixel_reveal','starfield','camera_shake','film_burn','ken_burns','letterbox_reveal','parallax_pan','spotlight_reveal','vignette_pulse','whip_pan','zoom_pulse','blinds_in','clock_wipe_in','cross_dissolve_in','fade_through_black_in','iris_in','morph_in','push_in','slide_wipe_in','zoom_through_in'].map(f=>`<option ${b.fx===f?'selected':''} value="${f}">${f}</option>`).join('')}</select>`},
  ];
  if(b.kind==='hook_title'){
    out.push({label:'kicker', html:`<input type="text" data-field="kicker" value="${esc(b.kicker||'')}">`});
    out.push({label:'title', html:`<input type="text" data-field="title" value="${esc(b.title||'')}">`});
    out.push({label:'vertical', html:`<input type="number" step="0.02" data-field="vertical" value="${b.vertical??0.78}">`});
    out.push({label:'logo_path', html:`<input type="text" data-field="logo_path" value="${esc(b.logo_path||'')}">`});
  } else if(b.kind==='word_pop' || b.kind==='bullet_burst'){
    out.push({label:'vertical', html:`<input type="number" step="0.02" data-field="vertical" value="${b.vertical??0.78}">`});
    out.push({label:'items (JSON)', html:`<textarea data-field="items">${esc(JSON.stringify(b.items||[], null, 1))}</textarea>`});
  } else if(b.kind==='stat_punch'){
    out.push({label:'value', html:`<input type="text" data-field="value" value="${esc(b.value||'')}">`});
    out.push({label:'caption', html:`<input type="text" data-field="caption" value="${esc(b.caption||'')}">`});
  } else if(b.kind==='quote_pull'){
    out.push({label:'quote_text', html:`<textarea data-field="quote_text">${esc(b.quote_text||'')}</textarea>`});
    out.push({label:'attribution', html:`<input type="text" data-field="attribution" value="${esc(b.attribution||'')}">`});
  } else if(b.kind==='image_card'){
    out.push({label:'image_path', html:`<input type="text" data-field="image_path" value="${esc(b.image_path||'')}">`});
    out.push({label:'card_top', html:`<input type="number" step="0.02" data-field="card_top" value="${b.card_top??0.6}">`});
    out.push({label:'card_fit', html:`<select data-field="card_fit"><option value="wide" ${b.card_fit==='wide'?'selected':''}>wide</option><option value="hug" ${b.card_fit==='hug'?'selected':''}>hug</option></select>`});
    out.push({label:'caption', html:`<input type="text" data-field="caption" value="${esc(b.caption||'')}">`});
  } else if(b.kind==='tool_logo_burst' || b.kind==='portrait_burst'){
    out.push({label:'vertical', html:`<input type="number" step="0.02" data-field="vertical" value="${b.vertical??0.45}">`});
    out.push({label:'items (JSON)', html:`<textarea data-field="items">${esc(JSON.stringify(b.items||[], null, 1))}</textarea>`});
  } else if(b.kind==='bar_overlay'){
    out.push({label:'title', html:`<input type="text" data-field="title" value="${esc(b.title||'')}">`});
    out.push({label:'vertical', html:`<input type="number" step="0.02" data-field="vertical" value="${b.vertical??0.55}">`});
    out.push({label:'bars (JSON)', html:`<textarea data-field="bars">${esc(JSON.stringify(b.bars||[], null, 1))}</textarea>`});
  } else if(b.kind==='ratio_dots'){
    out.push({label:'total', html:`<input type="number" data-field="total" value="${b.total||12}">`});
    out.push({label:'marked', html:`<input type="number" data-field="marked" value="${b.marked||9}">`});
    out.push({label:'polarity', html:`<select data-field="polarity"><option ${b.polarity==='negative'?'selected':''}>negative</option><option ${b.polarity==='positive'?'selected':''}>positive</option></select>`});
    out.push({label:'caption', html:`<input type="text" data-field="caption" value="${esc(b.caption||'')}">`});
  } else if(b.kind==='subscribe'){
    out.push({label:'vertical', html:`<input type="number" step="0.02" data-field="vertical" value="${b.vertical??0.88}">`});
  } else if(b.kind==='ring_chart'){
    out.push({label:'title', html:`<input type="text" data-field="title" value="${esc(b.title||'')}">`});
    out.push({label:'holeRatio', html:`<input type="number" step="0.05" min="0" max="0.85" data-field="holeRatio" value="${b.holeRatio??0.6}">`});
    out.push({label:'centerValue', html:`<input type="text" data-field="centerValue" value="${esc(b.centerValue||'')}">`});
    out.push({label:'centerLabel', html:`<input type="text" data-field="centerLabel" value="${esc(b.centerLabel||'')}">`});
    out.push({label:'segments (JSON)', html:`<textarea data-field="segments">${esc(JSON.stringify(b.segments||[], null, 1))}</textarea>`});
  } else if(b.kind==='countdown_reveal'){
    out.push({label:'steps (JSON)', html:`<textarea data-field="steps">${esc(JSON.stringify(b.steps||["3","2","1","GO"], null, 1))}</textarea>`});
    out.push({label:'subtitle', html:`<input type="text" data-field="subtitle" value="${esc(b.subtitle||'')}">`});
  } else if(b.kind==='logo_reveal_hero'){
    out.push({label:'image_path', html:`<input type="text" data-field="image_path" value="${esc(b.image_path||'')}">`});
    out.push({label:'name', html:`<input type="text" data-field="name" value="${esc(b.name||'')}">`});
    out.push({label:'tagline', html:`<input type="text" data-field="tagline" value="${esc(b.tagline||'')}">`});
  } else if(b.kind==='logo_reveal_style'){
    out.push({label:'image_path', html:`<input type="text" data-field="image_path" value="${esc(b.image_path||'')}">`});
    out.push({label:'name', html:`<input type="text" data-field="name" value="${esc(b.name||'')}">`});
    out.push({label:'tagline', html:`<input type="text" data-field="tagline" value="${esc(b.tagline||'')}">`});
    out.push({label:'style', html:`<select data-field="style">${['blur','bounce','fade','glitch','scale_rotate','split','stroke_draw','typewriter'].map(s=>`<option ${b.style===s?'selected':''} value="${s}">${s}</option>`).join('')}</select>`});
  } else if(b.kind==='image_compare_slider'){
    out.push({label:'before_image', html:`<input type="text" data-field="before_image" value="${esc(b.before_image||'')}">`});
    out.push({label:'after_image', html:`<input type="text" data-field="after_image" value="${esc(b.after_image||'')}">`});
    out.push({label:'before_label', html:`<input type="text" data-field="before_label" value="${esc(b.before_label||'')}">`});
    out.push({label:'after_label', html:`<input type="text" data-field="after_label" value="${esc(b.after_label||'')}">`});
  } else if(b.kind==='end_card'){
    out.push({label:'title', html:`<input type="text" data-field="title" value="${esc(b.title||'')}">`});
    out.push({label:'subtitle', html:`<input type="text" data-field="subtitle" value="${esc(b.subtitle||'')}">`});
    out.push({label:'cta', html:`<input type="text" data-field="cta" value="${esc(b.cta||'')}">`});
  } else if(b.kind==='gallery_grid' || b.kind==='masonry_gallery'){
    out.push({label:'images (JSON)', html:`<textarea data-field="images">${esc(JSON.stringify(b.images||[], null, 1))}</textarea>`});
    out.push({label:'caption', html:`<input type="text" data-field="caption" value="${esc(b.caption||'')}">`});
  } else if(b.kind==='image_carousel'){
    out.push({label:'images (JSON)', html:`<textarea data-field="images">${esc(JSON.stringify(b.images||[], null, 1))}</textarea>`});
    out.push({label:'labels (JSON)', html:`<textarea data-field="labels">${esc(JSON.stringify(b.labels||[], null, 1))}</textarea>`});
    out.push({label:'slot_sec', html:`<input type="number" step="0.1" data-field="slot_sec" value="${b.slot_sec??1.4}">`});
  } else if(b.kind==='image_zoom_reveal' || b.kind==='polaroid_frame'){
    out.push({label:'image', html:`<input type="text" data-field="image" value="${esc(b.image||'')}">`});
    out.push({label:'caption', html:`<input type="text" data-field="caption" value="${esc(b.caption||'')}">`});
  } else if(b.kind==='photo_stack'){
    out.push({label:'images (JSON)', html:`<textarea data-field="images">${esc(JSON.stringify(b.images||[], null, 1))}</textarea>`});
    out.push({label:'captions (JSON)', html:`<textarea data-field="captions">${esc(JSON.stringify(b.captions||[], null, 1))}</textarea>`});
  } else if(b.kind==='picture_in_picture'){
    out.push({label:'main_image', html:`<input type="text" data-field="main_image" value="${esc(b.main_image||'')}">`});
    out.push({label:'pip_image', html:`<input type="text" data-field="pip_image" value="${esc(b.pip_image||'')}">`});
    out.push({label:'pip_label', html:`<input type="text" data-field="pip_label" value="${esc(b.pip_label||'')}">`});
  } else if(b.kind==='split_panels'){
    out.push({label:'left_image', html:`<input type="text" data-field="left_image" value="${esc(b.left_image||'')}">`});
    out.push({label:'right_image', html:`<input type="text" data-field="right_image" value="${esc(b.right_image||'')}">`});
    out.push({label:'left_label', html:`<input type="text" data-field="left_label" value="${esc(b.left_label||'')}">`});
    out.push({label:'right_label', html:`<input type="text" data-field="right_label" value="${esc(b.right_label||'')}">`});
  } else if(b.kind==='area_chart'){
    out.push({label:'title', html:`<input type="text" data-field="title" value="${esc(b.title||'')}">`});
    out.push({label:'caption', html:`<input type="text" data-field="caption" value="${esc(b.caption||'')}">`});
    out.push({label:'chart_points (JSON)', html:`<textarea data-field="chart_points">${esc(JSON.stringify(b.chart_points||[], null, 1))}</textarea>`});
  } else if(b.kind==='progress_bars'){
    out.push({label:'title', html:`<input type="text" data-field="title" value="${esc(b.title||'')}">`});
    out.push({label:'bars (JSON)', html:`<textarea data-field="bars">${esc(JSON.stringify(b.bars||[], null, 1))}</textarea>`});
  } else if(b.kind==='stat_delta'){
    out.push({label:'pre_label', html:`<input type="text" data-field="pre_label" value="${esc(b.pre_label||'')}">`});
    out.push({label:'prefix', html:`<input type="text" data-field="prefix" value="${esc(b.prefix||'')}">`});
    out.push({label:'target', html:`<input type="number" data-field="target" value="${b.target??0}">`});
    out.push({label:'suffix', html:`<input type="text" data-field="suffix" value="${esc(b.suffix||'')}">`});
    out.push({label:'delta_value', html:`<input type="text" data-field="delta_value" value="${esc(b.delta_value||'')}">`});
    out.push({label:'delta_direction', html:`<select data-field="delta_direction"><option ${b.delta_direction==='up'?'selected':''} value="up">up</option><option ${b.delta_direction==='down'?'selected':''} value="down">down</option></select>`});
    out.push({label:'delta_label', html:`<input type="text" data-field="delta_label" value="${esc(b.delta_label||'')}">`});
  } else if(b.kind==='comparison_bars'){
    out.push({label:'title', html:`<input type="text" data-field="title" value="${esc(b.title||'')}">`});
    out.push({label:'before_label', html:`<input type="text" data-field="before_label" value="${esc(b.before_label||'')}">`});
    out.push({label:'after_label', html:`<input type="text" data-field="after_label" value="${esc(b.after_label||'')}">`});
    out.push({label:'comparison_rows (JSON)', html:`<textarea data-field="comparison_rows">${esc(JSON.stringify(b.comparison_rows||[], null, 1))}</textarea>`});
  } else if(b.kind==='circular_progress'){
    out.push({label:'value_pct', html:`<input type="number" min="0" max="100" data-field="value_pct" value="${b.value_pct??0}">`});
    out.push({label:'title', html:`<input type="text" data-field="title" value="${esc(b.title||'')}">`});
    out.push({label:'caption', html:`<input type="text" data-field="caption" value="${esc(b.caption||'')}">`});
  } else if(b.kind==='bounce_title'){
    out.push({label:'title', html:`<input type="text" data-field="title" value="${esc(b.title||'')}">`});
    out.push({label:'subtitle', html:`<input type="text" data-field="subtitle" value="${esc(b.subtitle||'')}">`});
  } else if(b.kind==='bubble_pop_text' || b.kind==='pop_text' || b.kind==='pulse_text' || b.kind==='text_sweep'){
    out.push({label:'quote_text', html:`<textarea data-field="quote_text">${esc(b.quote_text||'')}</textarea>`});
  } else if(b.kind==='typewriter_text'){
    out.push({label:'quote_text', html:`<textarea data-field="quote_text">${esc(b.quote_text||'')}</textarea>`});
    out.push({label:'chars_per_second', html:`<input type="number" data-field="chars_per_second" value="${b.chars_per_second??12}">`});
  } else if(b.kind==='list_reveal'){
    out.push({label:'title', html:`<input type="text" data-field="title" value="${esc(b.title||'')}">`});
    out.push({label:'items (JSON)', html:`<textarea data-field="items">${esc(JSON.stringify(b.items||[], null, 1))}</textarea>`});
  } else if(b.kind==='card_flip'){
    out.push({label:'front_text', html:`<input type="text" data-field="front_text" value="${esc(b.front_text||'')}">`});
    out.push({label:'back_text', html:`<input type="text" data-field="back_text" value="${esc(b.back_text||'')}">`});
    out.push({label:'front_label', html:`<input type="text" data-field="front_label" value="${esc(b.front_label||'')}">`});
    out.push({label:'back_label', html:`<input type="text" data-field="back_label" value="${esc(b.back_label||'')}">`});
    out.push({label:'flip_sec', html:`<input type="number" step="0.1" data-field="flip_sec" value="${b.flip_sec??1.5}">`});
  } else if(b.kind==='notification_stack'){
    out.push({label:'notifications (JSON)', html:`<textarea data-field="notifications">${esc(JSON.stringify(b.notifications||[], null, 1))}</textarea>`});
  } else if(b.kind==='carousel_3d'){
    out.push({label:'title', html:`<input type="text" data-field="title" value="${esc(b.title||'')}">`});
    out.push({label:'carousel_items (JSON)', html:`<textarea data-field="carousel_items">${esc(JSON.stringify(b.carousel_items||[], null, 1))}</textarea>`});
  } else if(b.kind==='sound_wave'){
    out.push({label:'caption', html:`<input type="text" data-field="caption" value="${esc(b.caption||'')}">`});
    out.push({label:'bar_count', html:`<input type="number" data-field="bar_count" value="${b.bar_count??24}">`});
  }
  return out;
}

async function updateField(field, value){
  if(selectedIdx<0) return;
  let v = value;
  if(field==='start_sec' || field==='end_sec' || field==='vertical' || field==='card_top' || field==='total' || field==='marked' || field==='holeRatio' || field==='value_pct' || field==='target' || field==='chars_per_second' || field==='flip_sec' || field==='bar_count') v = +value;
  if(field==='items' || field==='bars' || field==='segments' || field==='steps' || field==='images' || field==='labels' || field==='captions' || field==='chart_points' || field==='comparison_rows' || field==='notifications' || field==='carousel_items'){
    try{ v = JSON.parse(value); } catch(e){ return; }
  }
  currentPlan[selectedIdx][field] = v;
  await fetch('/plan/update',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid,idx:selectedIdx,patch:{[field]:v}})});
  renderTimeline();
}
async function addBeat(){
  const kind = $('add-kind').value;
  const r = await fetch('/plan/add',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid,kind,start_sec:videoDuration*0.4,duration:2.5})});
  const j = await r.json(); currentPlan = j.plan; selectedIdx = j.plan.length-1; renderTimeline(); renderBeatEditor();
}
async function dupBeat(){
  if(selectedIdx<0) return;
  const r = await fetch('/plan/duplicate',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid,idx:selectedIdx})});
  const j = await r.json(); currentPlan = j.plan; renderTimeline(); renderBeatEditor();
}
async function delBeat(){
  if(selectedIdx<0 || !confirm('Delete this beat?')) return;
  const r = await fetch('/plan/delete',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid,idx:selectedIdx})});
  const j = await r.json(); currentPlan = j.plan; selectedIdx = -1; renderTimeline(); renderBeatEditor();
}
async function planUndo(){
  const r = await fetch('/plan/undo',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid})});
  const j = await r.json(); if(j.plan){ currentPlan=j.plan; renderTimeline(); renderBeatEditor(); }
}
async function planRedo(){
  const r = await fetch('/plan/redo',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid})});
  const j = await r.json(); if(j.plan){ currentPlan=j.plan; renderTimeline(); renderBeatEditor(); }
}
async function applyTemplate(){
  const t = $('tmpl-sel').value; if(!t || !sid) return;
  if(!confirm('Replace current plan with template?')) return;
  const r = await fetch('/plan/template',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid,template:t})});
  const j = await r.json(); if(j.plan){ currentPlan=j.plan; selectedIdx=-1; renderTimeline(); renderBeatEditor(); }
}
async function rerender(){
  if(!sid) return;
  await fetch('/plan/render',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid})});
  pollEdit();
}
function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
document.addEventListener('keydown', e=>{
  if((e.ctrlKey||e.metaKey) && e.key==='z' && !e.shiftKey){ if(sid){ e.preventDefault(); planUndo(); } }
  if((e.ctrlKey||e.metaKey) && (e.key==='y' || (e.shiftKey && e.key==='Z'))){ if(sid){ e.preventDefault(); planRedo(); } }
});

// ───────────── Asset modals ─────────────
function openModal(id){ $(id).classList.add('show'); }
function closeModal(id){ $(id).classList.remove('show'); }
async function logoFetch(){
  const brand = $('logo-q').value.trim(); if(!brand) return;
  $('logo-result').textContent = 'Fetching...';
  const r = await fetch('/asset/logo',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({brand})});
  const j = await r.json();
  if(j.error){ $('logo-result').textContent = '❌ '+j.error; return; }
  $('logo-result').innerHTML = `✓ Fetched <code>${j.file}</code><br><img src="${j.url}" style="max-width:120px;margin-top:8px;background:#fff;padding:8px;border-radius:6px"><br><button class="primary" style="margin-top:10px" onclick="useLogoAsAsset('${j.file}')">Use in selected beat</button>`;
}
function useLogoAsAsset(file){
  if(selectedIdx<0){ alert('Select a beat first'); return; }
  const b = currentPlan[selectedIdx];
  if(b.kind==='tool_logo_burst'){
    (b.items=b.items||[]).push({image_path:file, label:file.replace('.png','').toUpperCase(), appear_sec:+b.start_sec});
    updateField('items', JSON.stringify(b.items));
  } else if(b.kind==='hook_title'){
    updateField('logo_path', file);
  } else if(b.kind==='image_card'){
    updateField('image_path', file);
  }
  closeModal('modal-logo'); renderBeatEditor();
}
async function stockSearch(){
  const q = $('stock-q').value.trim(); if(!q) return;
  $('stock-thumbs').innerHTML = 'Searching...';
  const r = await fetch('/stock?q='+encodeURIComponent(q)); const j = await r.json();
  $('stock-thumbs').innerHTML = '';
  if(!j.results || !j.results.length){ $('stock-thumbs').innerHTML = '<div style="color:#7a8497">No results</div>'; return; }
  j.results.forEach(p=>{
    const img = document.createElement('img');
    img.src = p.thumb; img.title = 'by '+p.photographer;
    img.addEventListener('click', ()=>useStockImage(p.full, q));
    $('stock-thumbs').appendChild(img);
  });
}
async function useStockImage(url, name){
  if(selectedIdx<0){ alert('Select a beat first'); return; }
  const r = await fetch('/asset/stock-pick',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sid,url,name})});
  const j = await r.json();
  if(j.image_path){ updateField('image_path', j.image_path); closeModal('modal-stock'); }
}
async function uploadAsset(){
  if(selectedIdx<0){ alert('Select a beat first'); return; }
  const inp = document.createElement('input'); inp.type='file'; inp.accept='image/*';
  inp.onchange = async () => {
    const f = inp.files[0]; if(!f) return;
    const buf = await f.arrayBuffer();
    const r = await fetch('/asset/upload?sid='+sid,{method:'POST',headers:{'X-Filename':f.name},body:buf});
    const j = await r.json();
    if(j.image_path){ updateField('image_path', j.image_path); }
  };
  inp.click();
}
async function loadMusic(){
  const r = await fetch('/music'); const j = await r.json();
  $('music-list').innerHTML = '';
  (j.tracks||[]).forEach(t=>{
    const row = document.createElement('div'); row.className='music-row';
    row.innerHTML = `<div class="nm">${t}</div><audio controls preload="none" src="/music/file?name=${encodeURIComponent(t)}"></audio>`;
    $('music-list').appendChild(row);
  });
}
async function pollEdit(){
  const j = await fetchState(); if(!j) return;
  renderEdit(j);
  if(j.chat) renderChat(j.chat);
  if(j.status==='edit:done' || j.status==='edit:error'){
    $('b-edit').disabled=false; $('b-edit').textContent='Re-edit';
    if(j.status==='edit:done') loadPlan();   // re-sync plan from disk (picks up Claude's queue edits)
    return;
  }
  setTimeout(pollEdit, 1500);
}
function progressBarHtml(p){
  if(!p || !p.total){
    return `<div class="status"><span class="ok">⚡ starting render…</span></div>`;
  }
  const pct = Math.min(100, Math.round((p.current / p.total) * 100));
  const phaseLabel = p.phase === 'encoding' ? 'Encoding' : p.phase === 'rendering' ? 'Rendering frames' : p.phase;
  const eta = p.eta_sec != null ? ` · ~${p.eta_sec}s left` : '';
  return `
    <div class="status" style="padding:0;background:transparent">
      <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-bottom:6px">
        <span>${phaseLabel} — ${p.current}/${p.total}</span>
        <span>${pct}%${eta}</span>
      </div>
      <div style="height:8px;background:var(--input-bg);border-radius:4px;overflow:hidden;border:1px solid var(--input-border)">
        <div style="height:100%;width:${pct}%;background:var(--accent);transition:width .3s ease"></div>
      </div>
    </div>`;
}
let lastPreviewScrolledFor = null;
function renderEdit(j){
  const div = $('edit-result');
  if(j.status==='edit:planning'){
    div.classList.remove('hidden');
    div.innerHTML = `<div class="card"><h2>Planning</h2><div class="status"><span class="ok">⚡ ${j.transcribe_phase || 'Transcribing...'}</span></div></div>`;
    return;
  }
  if(j.status==='edit:rendering'){
    div.classList.remove('hidden');
    div.innerHTML = `<div class="card"><h2>Rendering</h2>${progressBarHtml(j.render_progress)}</div>`;
    return;
  }
  if(j.status==='edit:done' && j.preview_path){
    div.classList.remove('hidden');
    div.innerHTML = `<div class="card"><h2>Preview</h2>
      <video class="player" controls src="/file?p=${encodeURIComponent(j.preview_path)}&t=${Date.now()}"></video>
    </div>`;
    attachLiveAudio(div.querySelector('video.player'));
    // Scroll the finished preview into view automatically instead of
    // leaving the user staring at wherever the page happened to be
    // scrolled — only once per preview (not on every poll tick), keyed
    // on the path so a genuinely new preview scrolls again.
    if(lastPreviewScrolledFor !== j.preview_path){
      lastPreviewScrolledFor = j.preview_path;
      setTimeout(()=>{ div.scrollIntoView({behavior:'smooth', block:'start'}); }, 50);
    }
  }
}
function renderChat(chat){
  const div=$('chat'); div.innerHTML='';
  for(const m of chat){
    const cls = m.role==='user' ? 'user' : 'assistant';
    div.innerHTML += `<div class="msg ${cls}">${md(m.text)}</div>`;
  }
  div.scrollTop = div.scrollHeight;
}
function md(t){ return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\n/g,'<br>'); }
async function sendChat(){
  const t = $('chatmsg').value.trim(); if(!t || !sid) return;
  $('chatmsg').value='';
  await fetch('/chat', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid, text:t})});
  pollEdit();
}
async function setAspect(){
  if(!sid) return;
  const aspect = $('aspect-sel').value;
  await fetch('/aspect', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid, aspect})});
}
async function openTools(){
  const [rs, rt] = await Promise.all([
    fetch('/tools/studio', {method:'POST'}).then(r=>r.json()).catch(()=>({ok:false})),
    fetch('/tools/tuner', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid})}).then(r=>r.json()).catch(()=>({ok:false})),
  ]);
  if(!rs.ok) toast('⚠ Could not start Remotion Studio — check ⚠ error log');
  if(!rt.ok) toast('⚠ Could not start Position Tuner — check ⚠ error log');
  // tuner (plain Flask) boots almost instantly; Remotion Studio runs a webpack
  // build first, so give it real time to actually bind before opening its tab.
  if(rt.ok) window.open('http://localhost:5058','_blank');
  if(rs.ok) setTimeout(()=>window.open('http://localhost:5057','_blank'), 3500);
}

async function runFinal(){
  if(!sid){ alert('Run Edit first'); return; }
  if(!confirm('Render final at '+preset+' quality?')) return;
  $('b-final').disabled=true; $('b-final').textContent='Rendering...';
  await fetch('/export/start', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid, preset})});
  pollFinal();
}
async function pollFinal(){
  const j = await fetchState(); if(!j) return;
  const div=$('export-result'); div.classList.remove('hidden');
  if(j.status==='export:done' && j.final_path){
    div.innerHTML = `<div class="card"><h2>🎬 Final ready</h2>
      <video class="player" controls src="/file?p=${encodeURIComponent(j.final_path)}&t=${Date.now()}"></video>
      <a class="dl" href="/file?p=${encodeURIComponent(j.final_path)}&dl=1" download>Download final .mp4</a></div>`;
    $('b-final').disabled=false; $('b-final').textContent='Re-render';
    return;
  }
  if(j.status==='export:error'){
    div.innerHTML = `<div class="card"><div class="status"><span class="err">${j.error}</span></div></div>`;
    $('b-final').disabled=false; $('b-final').textContent='Try again';
    return;
  }
  div.innerHTML = `<div class="card"><h2>Rendering final</h2>${progressBarHtml(j.render_progress)}</div>`;
  setTimeout(pollFinal, 1000);
}
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # Every poller (pollClean/pollEdit/pollFinal/fetchState) hits the
        # EXACT SAME URL (/state?sid=X) repeatedly with no cache-busting --
        # without this header nothing stops the browser from silently
        # serving a stale cached response instead of asking the server.
        # Confirmed directly: a real browser test showed a progress bar
        # frozen mid-render (network tab still showing requests "succeeding")
        # while the server had already finished and moved on -- this is very
        # likely the actual mechanism behind every "looks frozen" report,
        # not just the missing-progress-data bugs fixed elsewhere.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if u.path == "/state":
            sid = parse_qs(u.query).get("sid", [""])[0]
            j = SESSIONS.get(sid)
            if not j: self._json(404, {"error": "no session"}); return
            self._json(200, j); return
        if u.path == "/waveform":
            sid = parse_qs(u.query).get("sid", [""])[0]
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            src = Path(sess["src"])
            wf = sess.get("waveform")
            if not wf:
                wf = extract_waveform(src, samples=400)
                sess["waveform"] = wf
                sess["wf_duration"] = probe_duration(src)
                sess["wf_silences"] = detect_silences(src, sess.get("noise_db", -32), sess.get("min_gap", 0.30))
            self._json(200, {"peaks": wf, "duration": sess.get("wf_duration"),
                             "silences": sess.get("wf_silences", [])}); return

        if u.path == "/transcript":
            sid = parse_qs(u.query).get("sid", [""])[0]
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            self._json(200, {"words": sess.get("words", []),
                             "manual_cuts": sess.get("manual_cuts", [])}); return

        if u.path == "/plan":
            sid = parse_qs(u.query).get("sid", [""])[0]
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            plan = load_plan(sid)
            src = Path(sess.get("clean_path") or sess.get("src", "")).resolve()
            dur = probe_duration(src) if src.exists() else 0
            self._json(200, {"plan": plan, "duration": dur, "templates": [
                {"id": k, "name": v["name"]} for k, v in PLAN_TEMPLATES.items()]}); return

        if u.path == "/music":
            self._json(200, {"tracks": list_music_tracks()}); return

        if u.path == "/music/file":
            name = parse_qs(u.query).get("name", [""])[0]
            p = SKILL / "assets" / name
            if not p.exists() or not name.endswith(".mp3"):
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(p.stat().st_size))
            self.end_headers()
            with p.open("rb") as f: shutil.copyfileobj(f, self.wfile)
            return

        if u.path == "/logos":
            self._json(200, {"logos": list_logos()}); return

        if u.path == "/asset":
            q = parse_qs(u.query); kind = q.get("kind", [""])[0]; path = q.get("path", [""])[0]
            if kind == "logo": p = SKILL / "assets" / "logos" / path
            else: p = Path(path)
            if not p.exists(): self.send_response(404); self.end_headers(); return
            self.send_response(200)
            mime = "image/png" if path.endswith(".png") else "image/jpeg"
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(p.stat().st_size))
            self.end_headers()
            with p.open("rb") as f: shutil.copyfileobj(f, self.wfile)
            return

        if u.path == "/stock":
            q = parse_qs(u.query).get("q", [""])[0]
            results = pexels_search(q) if q else []
            self._json(200, {"results": results}); return

        if u.path == "/errors":
            self._json(200, {"errors": ERROR_LOG[-50:]}); return

        if u.path == "/projects":
            self._json(200, {"projects": list_projects()}); return

        if u.path == "/thumb":
            p = Path(parse_qs(u.query).get("p", [""])[0])
            if not p.exists(): self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(p.stat().st_size))
            self.end_headers()
            with p.open("rb") as f: shutil.copyfileobj(f, self.wfile)
            return

        if u.path == "/file":
            q = parse_qs(u.query)
            p = Path(q.get("p", [""])[0])
            if not p.exists(): self.send_response(404); self.end_headers(); return
            size = p.stat().st_size
            is_download = q.get("dl", ["0"])[0] == "1"
            # <video> elements normally issue HTTP Range requests to stream/
            # seek instead of pulling the whole file up front — without
            # Range support the browser may be forced into a full-buffer
            # load before any playback starts, which on a large clean.mp4
            # (100+ MB) can look and behave just like an unwanted automatic
            # download even though no download was actually triggered.
            range_header = self.headers.get("Range")
            if range_header and not is_download:
                try:
                    range_val = range_header.split("=", 1)[1]
                    start_s, end_s = range_val.split("-", 1)
                    start = int(start_s)
                    end = int(end_s) if end_s else size - 1
                    end = min(end, size - 1)
                    self.send_response(206)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(end - start + 1))
                    self.end_headers()
                    with p.open("rb") as f:
                        f.seek(start)
                        remaining = end - start + 1
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk: break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    return
                except Exception:
                    pass  # fall through to a plain full-file response below
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            # only force download when explicitly asked (dl=1) — the <video> player
            # needs this endpoint to serve inline, not as an attachment
            if is_download:
                # Header values are latin-1 only; macOS time-stamped names carry
                # U+202F (narrow no-break space). Give an ASCII fallback + RFC 5987
                # UTF-8 name so browsers still get the real filename.
                ascii_name = p.name.encode("ascii", "ignore").decode("ascii") or "download"
                self.send_header("Content-Disposition",
                                 f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(p.name)}")
            self.end_headers()
            with p.open("rb") as f: shutil.copyfileobj(f, self.wfile)
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n)) if n else {}

        if u.path == "/clean/start":
            # Multi-clip mode: body["sources"] is a list of paths
            sources_in = body.get("sources") or []
            if not sources_in and body.get("src"): sources_in = [body["src"]]
            resolved = []
            for s in sources_in:
                p = Path(s)
                if not p.exists():
                    cand = Path.home() / "Downloads" / Path(s).name
                    if cand.exists(): p = cand
                if p.exists(): resolved.append(str(p.resolve()))
                else:
                    self._json(400, {"error": f"file not found: {s}"}); return
            if not resolved: self._json(400, {"error": "no sources"}); return
            src = Path(resolved[0])
            sid = uuid.uuid4().hex
            # This is the FIRST clean for a brand-new session — the client
            # ALWAYS sends explicit noise_db/min_gap values (the sliders'
            # current DOM value, never absent), but for a video the user
            # hasn't seen a waveform for yet, those are just untouched
            # defaults, not an informed choice. Calibrate both to the
            # actual recording instead of trusting one fixed pair for every
            # upload. 0.3s min_gap was verified directly to be too
            # conservative for real conversational speech: on a real test
            # recording it found 4 pause-gaps vs. 20 at 0.1s for the exact
            # same audio — most real pauses in fast/natural speech are
            # under 0.3s and were being missed entirely. Subsequent
            # /clean/reclean calls respect the user's explicit knob choice.
            try:
                total_dur0 = probe_duration(src)
                calibrated_min_gap = 0.12
                calibrated_noise_db = auto_noise_db(src, calibrated_min_gap, total_dur0)
            except Exception:
                calibrated_min_gap = body.get("min_gap", 0.30)
                calibrated_noise_db = body.get("noise_db", -32)
            SESSIONS[sid] = {
                "src": str(src.resolve()),
                "sources": resolved,
                "noise_db": calibrated_noise_db,
                "min_gap": calibrated_min_gap,
                "target_gap": body.get("target_gap", 0.30),
                "cut_head": body.get("cut_head", True),
                "cut_tail": body.get("cut_tail", True),
                "head_buffer_sec": body.get("head_buffer_sec", 0.5),
                "tail_buffer_sec": body.get("tail_buffer_sec", 0.5),
                "remove_retakes": body.get("remove_retakes", True),
                "remove_clicks": body.get("remove_clicks", False),
                "stabilize": body.get("stabilize", False),
                "camera_movement": body.get("camera_movement", "none"),
                "denoise_intensity": body.get("denoise_intensity", 0.0),
                "enhance_intensity": body.get("enhance_intensity", 0.0),
                "volume_pct": body.get("volume_pct", 100.0),
                "manual_cuts": body.get("manual_cuts", []),
                "status": "clean:queued", "chat": [], "history": [],
            }
            threading.Thread(target=job_clean, args=(sid,), daemon=True).start()
            self._json(200, {"sid": sid}); return

        if u.path == "/clean/reclean":
            sid = body.get("sid")
            if sid not in SESSIONS: self._json(404, {"error": "no session"}); return
            sess = SESSIONS[sid]
            CUT_AFFECTING_KEYS = ("noise_db", "min_gap", "target_gap", "cut_head", "cut_tail",
                                   "head_buffer_sec", "tail_buffer_sec",
                                   "remove_retakes", "remove_clicks")
            # stabilize/camera_movement no longer run as part of Clean/Re-clean
            # at all -- they're a separate, explicit "Apply Camera Effects" step
            # (POST /clean/apply_camera_effects) the user triggers on the
            # already-cleaned video, once, only if they actually want it. So
            # changing these settings here just updates the saved toggle value
            # for that later step -- it must NOT force a full re-splice (the
            # base cut is completely unaffected by them now).
            SAVED_ONLY_KEYS = ("stabilize", "camera_movement", "camera_movement_intensity")
            # Adjusting ONLY denoise/enhance doesn't change which video
            # segments survive the cut at all — re-running the full
            # silence/retake analysis + re-encoding the entire video for a
            # pure audio-processing tweak is expensive and unnecessary.
            # Detect that case and take the cheap path: re-apply just the
            # audio filter to the ALREADY-SPLICED clean.mp4 with the video
            # stream copied (not re-encoded) instead of redoing everything.
            cut_params_changed = any(
                k in body and body[k] != sess.get(k) for k in CUT_AFFECTING_KEYS
            )
            manual_cuts_changed = "manual_cuts" in body and body["manual_cuts"] != sess.get("manual_cuts")
            audio_only = (
                not cut_params_changed and not manual_cuts_changed
                and sess.get("clean_path") and Path(sess["clean_path"]).exists()
                and ("denoise_intensity" in body or "enhance_intensity" in body or "volume_pct" in body)
            )
            for k in CUT_AFFECTING_KEYS + SAVED_ONLY_KEYS + ("denoise_intensity", "enhance_intensity", "volume_pct"):
                if k in body: sess[k] = body[k]
            if "manual_cuts" in body: sess["manual_cuts"] = body["manual_cuts"]
            if audio_only:
                threading.Thread(target=job_audio_only_finish, args=(sid,), daemon=True).start()
            else:
                # Re-clean (adjusting knobs after the transcript's already been
                # approved once) goes straight to the analyze/splice stage — the
                # transcript itself doesn't need re-transcribing or re-approving
                # just because a silence/retake knob changed.
                threading.Thread(target=job_clean_finish, args=(sid,), daemon=True).start()
            self._json(200, {"ok": True, "audio_only": audio_only}); return

        if u.path == "/clean/apply_camera_effects":
            # Explicit, separate action -- stabilize/camera_movement run
            # here and ONLY here, on the already-cleaned base video, never
            # automatically as part of /clean/start or /clean/reclean.
            sid = body.get("sid")
            if sid not in SESSIONS: self._json(404, {"error": "no session"}); return
            sess = SESSIONS[sid]
            if sess.get("status") != "clean:done":
                self._json(400, {"error": "clean the video first"}); return
            if "stabilize" in body: sess["stabilize"] = body["stabilize"]
            if "camera_movement" in body: sess["camera_movement"] = body["camera_movement"]
            if "camera_movement_intensity" in body: sess["camera_movement_intensity"] = body["camera_movement_intensity"]
            threading.Thread(target=job_apply_camera_effects, args=(sid,), daemon=True).start()
            self._json(200, {"ok": True}); return

        if u.path == "/clean/approve":
            # First transition out of "awaiting_approval" — the user has
            # reviewed (and possibly corrected via double-click) the
            # transcript and is ready for the actual cut/render to happen.
            sid = body.get("sid")
            if sid not in SESSIONS: self._json(404, {"error": "no session"}); return
            sess = SESSIONS[sid]
            if sess.get("status") != "clean:awaiting_approval":
                self._json(400, {"error": "not awaiting approval"}); return
            threading.Thread(target=job_clean_finish, args=(sid,), daemon=True).start()
            self._json(200, {"ok": True}); return

        if u.path == "/clean/set_audio_params":
            # Just persists the numbers — no render, no ffmpeg call. Volume
            # and enhance are already live in the browser's own Web Audio
            # graph; this only needs to remember the values so bake_audio
            # (called once, lazily, right before download/edit) uses them.
            sid = body.get("sid")
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            for k in ("denoise_intensity", "enhance_intensity", "volume_pct"):
                if k in body: sess[k] = body[k]
            self._json(200, {"ok": True}); return

        if u.path == "/clean/bake_audio":
            # The one point audio params actually get encoded into the real
            # file — called right before the user needs it (download button,
            # or moving to Edit), not on every slider tick. Synchronous
            # (blocks until done) since the client awaits this before
            # triggering the download / tab switch.
            sid = body.get("sid")
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            clean_path = sess.get("clean_path")
            if not clean_path or not Path(clean_path).exists():
                self._json(200, {"ok": True, "skipped": "no clean_path yet"}); return
            raw = Path(str(clean_path) + ".raw.mp4")
            if not raw.exists():
                self._json(200, {"ok": True, "skipped": "no raw intermediate"}); return
            try:
                audio_filter = build_audio_chain(
                    sess.get("denoise_intensity", 0), sess.get("enhance_intensity", 0),
                    noise_floor_db=sess.get("noise_db"), volume_pct=sess.get("volume_pct", 100.0))
                apply_audio_filter(raw, Path(clean_path), audio_filter)
                self._json(200, {"ok": True}); return
            except Exception as e:
                self._json(500, {"error": str(e)}); return

        if u.path == "/clean/undo":
            sid = body.get("sid")
            sess = SESSIONS.get(sid)
            if not sess or len(sess.get("history", [])) < 2:
                self._json(400, {"error": "nothing to undo"}); return
            sess["history"].pop()  # drop current
            prev = sess["history"][-1]
            for k, v in prev.items(): sess[k] = v
            # Undo re-applies a previous knob configuration — the transcript
            # was already approved earlier in this session, so this goes
            # straight to analyze/splice like reclean does.
            threading.Thread(target=job_clean_finish, args=(sid,), daemon=True).start()
            self._json(200, {"ok": True}); return

        if u.path == "/transcript/cut":
            sid = body.get("sid")
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            words = sess.get("words", [])
            if "word_idx" in body:
                # Padding computed server-side, clamped to the actual gap
                # to the neighboring word — see pad_span_safe(). A blind
                # client-computed +/-0.04s (the old behavior) routinely bit
                # into the next word in fast speech, truncating it.
                idx = body["word_idx"]
                if not (0 <= idx < len(words)):
                    self._json(400, {"error": "bad word_idx"}); return
                start, end = pad_span_safe(words, idx, idx, 0.04)
            else:
                start, end = body["start"], body["end"]
            mc = sess.setdefault("manual_cuts", [])
            mc.append([start, end])
            self._json(200, {"manual_cuts": mc}); return

        if u.path == "/transcript/restore":
            sid = body.get("sid")
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            idx = body.get("idx", -1)
            mc = sess.get("manual_cuts", [])
            if 0 <= idx < len(mc): mc.pop(idx)
            self._json(200, {"manual_cuts": mc}); return

        if u.path == "/transcript/edit_word":
            # Corrects a misheard/wrong word's TEXT in place (start/end
            # timestamps untouched — the ASR alignment timing is still
            # right even when the word itself was misheard). Persisted to
            # the on-disk words.json in the workdir, not just in-memory, so
            # every downstream consumer that reads the transcript later
            # (captions_plan generation, retake detection, re-editing) sees
            # the correction — fixing it once here, not once per feature.
            sid = body.get("sid")
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            idx = body.get("idx", -1)
            new_text = str(body.get("text", "")).strip()
            words = sess.get("words", [])
            if not new_text or not (0 <= idx < len(words)):
                self._json(400, {"error": "bad index or empty text"}); return
            words[idx]["word"] = new_text
            sess["words"] = words
            wd = sess.get("workdir")
            if wd:
                wp = Path(wd) / "words.json"
                try:
                    wp.write_text(json.dumps(words, indent=2, ensure_ascii=True), encoding="ascii")
                except Exception:
                    pass
            self._json(200, {"words": words}); return

        if u.path == "/edit/start":
            sid = body.get("sid")
            if sid not in SESSIONS: self._json(404, {"error": "no session"}); return
            sess = SESSIONS[sid]
            if body.get("src"): sess["clean_path"] = body["src"]
            threading.Thread(target=job_edit, args=(sid,), daemon=True).start()
            self._json(200, {"ok": True}); return

        if u.path == "/chat":
            sid = body.get("sid")
            if sid not in SESSIONS: self._json(404, {"error": "no session"}); return
            text = body.get("text", "").strip()
            append_chat(sid, "user", text)
            qp = enqueue_prompt(sid, text) if text else None
            if qp:
                append_chat(sid, "assistant",
                            f"**Queued for Claude.** In your terminal, tell Claude: "
                            f"*\"apply the queue for `{qp}`\"*. When it's done, click "
                            f"**⟳ Load Claude's edits**, then **Re-render preview**.", "msg")
            else:
                append_chat(sid, "assistant",
                            "Run **Auto-edit** first so there's a workdir to queue into.", "error")
            self._json(200, {"ok": True}); return

        if u.path == "/aspect":
            sid = body.get("sid")
            if sid not in SESSIONS: self._json(404, {"error": "no session"}); return
            val = body.get("aspect", "auto")
            if val not in ("auto", "16:9"): val = "auto"
            SESSIONS[sid]["aspect"] = val
            self._json(200, {"aspect": val}); return

        if u.path == "/export/start":
            sid = body.get("sid")
            if sid not in SESSIONS: self._json(404, {"error": "no session"}); return
            threading.Thread(target=job_final, args=(sid,), daemon=True).start()
            self._json(200, {"ok": True}); return

        if u.path == "/tools/studio":
            ok = launch_studio(); self._json(200, {"ok": ok}); return
        if u.path == "/tools/tuner":
            sess = SESSIONS.get(body.get("sid"))
            wd = str(edit_workdir_for(sess)) if sess else None
            ok = launch_tuner(wd); self._json(200, {"ok": ok}); return

        # ───── Plan editor endpoints ─────
        if u.path == "/plan/update":
            sid = body.get("sid"); idx = body.get("idx", -1); patch = body.get("patch", {})
            plan = load_plan(sid)
            if 0 <= idx < len(plan):
                plan[idx].update(patch); save_plan(sid, plan); self._json(200, {"plan": plan})
            else: self._json(400, {"error": "bad index"})
            return

        if u.path == "/plan/add":
            sid = body.get("sid"); kind = body.get("kind", "word_pop")
            start = float(body.get("start_sec", 1.0)); dur = float(body.get("duration", 2.5))
            plan = load_plan(sid)
            beat = {"kind": kind, "start_sec": start, "end_sec": start + dur, "reason": "Added via editor"}
            beat.update(DEFAULT_BEAT_FIELDS.get(kind, {}))
            plan.append(beat); plan.sort(key=lambda b: b.get("start_sec", 0))
            save_plan(sid, plan); self._json(200, {"plan": plan}); return

        if u.path == "/plan/delete":
            sid = body.get("sid"); idx = body.get("idx", -1)
            plan = load_plan(sid)
            if 0 <= idx < len(plan):
                plan.pop(idx); save_plan(sid, plan)
            self._json(200, {"plan": plan}); return

        if u.path == "/plan/duplicate":
            sid = body.get("sid"); idx = body.get("idx", -1)
            plan = load_plan(sid)
            if 0 <= idx < len(plan):
                clone = json.loads(json.dumps(plan[idx]))
                shift = (clone.get("end_sec", 0) - clone.get("start_sec", 0)) + 0.3
                clone["start_sec"] = float(clone.get("start_sec", 0)) + shift
                clone["end_sec"] = float(clone.get("end_sec", 0)) + shift
                plan.insert(idx + 1, clone)
                save_plan(sid, plan)
            self._json(200, {"plan": plan}); return

        if u.path == "/plan/move":
            sid = body.get("sid"); idx = body.get("idx", -1)
            new_start = float(body.get("start_sec", 0))
            plan = load_plan(sid)
            if 0 <= idx < len(plan):
                cur_dur = float(plan[idx].get("end_sec", 0)) - float(plan[idx].get("start_sec", 0))
                plan[idx]["start_sec"] = round(new_start, 2)
                plan[idx]["end_sec"] = round(new_start + cur_dur, 2)
                save_plan(sid, plan)
            self._json(200, {"plan": plan}); return

        if u.path == "/plan/resize":
            sid = body.get("sid"); idx = body.get("idx", -1)
            edge = body.get("edge", "end"); new_t = float(body.get("t", 0))
            plan = load_plan(sid)
            if 0 <= idx < len(plan):
                MIN_DUR = 0.1
                if edge == "start":
                    plan[idx]["start_sec"] = round(min(new_t, plan[idx].get("end_sec", 0) - MIN_DUR), 2)
                else:
                    plan[idx]["end_sec"] = round(max(new_t, plan[idx].get("start_sec", 0) + MIN_DUR), 2)
                save_plan(sid, plan)
            self._json(200, {"plan": plan}); return

        if u.path == "/plan/undo":
            sid = body.get("sid")
            hist = PLAN_HISTORY.get(sid, [])
            if not hist: self._json(400, {"error": "nothing to undo"}); return
            cur = load_plan(sid)
            PLAN_FUTURE.setdefault(sid, []).append(cur)
            prev = hist.pop()
            save_plan(sid, prev, push_history=False)
            self._json(200, {"plan": prev}); return

        if u.path == "/plan/redo":
            sid = body.get("sid")
            fut = PLAN_FUTURE.get(sid, [])
            if not fut: self._json(400, {"error": "nothing to redo"}); return
            cur = load_plan(sid)
            PLAN_HISTORY.setdefault(sid, []).append(cur)
            nxt = fut.pop()
            save_plan(sid, nxt, push_history=False)
            self._json(200, {"plan": nxt}); return

        if u.path == "/plan/template":
            sid = body.get("sid"); name = body.get("template")
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            tmpl = PLAN_TEMPLATES.get(name)
            if not tmpl: self._json(400, {"error": "unknown template"}); return
            src = Path(sess.get("clean_path") or sess["src"]).resolve()
            dur = probe_duration(src)
            beats = tmpl["beats_for"](dur)
            for b in beats:
                b["start_sec"] = round(float(b.get("start_sec", 0)), 2)
                b["end_sec"] = round(float(b.get("end_sec", 0)), 2)
            # ensure edit_workdir
            wd = workdir_for(src); sess["edit_workdir"] = str(wd)
            save_plan(sid, beats)
            self._json(200, {"plan": beats}); return

        if u.path == "/plan/render":
            sid = body.get("sid")
            threading.Thread(target=lambda: _just_render(sid), daemon=True).start()
            self._json(200, {"ok": True}); return

        # ───── Asset endpoints ─────
        if u.path == "/asset/logo":
            brand = body.get("brand", "").strip()
            if not brand: self._json(400, {"error": "no brand"}); return
            slug = fetch_logo_brand(brand)
            if slug: self._json(200, {"file": slug, "url": f"/asset?path={slug}&kind=logo"})
            else: self._json(404, {"error": f"no logo found for {brand}"}); return

        if u.path == "/asset/stock-pick":
            sid = body.get("sid"); url = body.get("url"); name = body.get("name", "stock")
            path = pexels_download(url, sid, name)
            self._json(200 if path else 500, {"image_path": path} if path else {"error": "download failed"}); return

        # ───── Project save/load + errors ─────
        if u.path == "/project/save":
            sid = body.get("sid")
            p = save_project(sid)
            self._json(200, {"path": str(p) if p else None}); return

        if u.path == "/project/open":
            path = body.get("path", "")
            new_sid = load_project(Path(path))
            self._json(200 if new_sid else 404, {"sid": new_sid} if new_sid else {"error": "load failed"}); return

        if u.path == "/project/delete":
            ok = delete_project(Path(body.get("path", "")))
            self._json(200, {"ok": ok}); return

        if u.path == "/asset/upload":
            # multipart upload handled inline (basic parser)
            sid = parse_qs(urlparse(self.path).query).get("sid", [""])[0]
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            # only support simple raw-body upload w/ filename header
            fname = self.headers.get("X-Filename", f"upload_{int(time.time())}.bin")
            if Path(fname).suffix.lower() not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                self._json(400, {"error": "unsupported image type"}); return
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0:
                self._json(400, {"error": "empty upload"}); return
            wd = edit_workdir_for(sess)
            broll = wd / "broll"; broll.mkdir(parents=True, exist_ok=True)
            (broll / fname).write_bytes(self.rfile.read(n))
            self._json(200, {"image_path": f"broll/{fname}"}); return

        self.send_response(404); self.end_headers()


if __name__ == "__main__":
    port = int(os.getenv("STUDIO_PORT", "5056"))  # 5000 is taken by macOS AirPlay Receiver
    print(f"Video Studio: http://localhost:{port}")
    # ThreadingHTTPServer, not plain HTTPServer — the plain single-threaded
    # server processes one request at a time. A render's status polling
    # (/state every 1.5s from the browser) would queue up behind it and the
    # server could stop accepting new connections entirely under load —
    # exactly why progress polling appeared to freeze during a render.
    server = ThreadingHTTPServer(("localhost", port), H)
    server.daemon_threads = True
    server.serve_forever()
