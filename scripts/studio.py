"""Video Studio — unified UI for Clean + Edit + Export.
http://localhost:5000

One server. Three phases via tabs:
  CLEAN  — gap/silence/retake removal (was cleaner_app.py)
  EDIT   — auto-plan + render preview + chat refine (was editor_app.py)
  EXPORT — final 1080p render + download
"""
from __future__ import annotations
import json, re, hashlib, subprocess, threading, uuid, shutil, time, os, socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SKILL = Path(__file__).resolve().parent.parent
VENV_PY = SKILL / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python3")
TRANSCRIBE = SKILL / "scripts/transcribe.py"
RENDER_SH = SKILL / "scripts/render.sh"
WORK_ROOT = Path.home() / ".cache/video-edit"
WORK_ROOT.mkdir(parents=True, exist_ok=True)

SESSIONS: dict[str, dict] = {}


def _run(cmd, **kw): return subprocess.run(cmd, **kw)


def combine_clips(paths: list[str], dest: Path) -> Path:
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
    _run(cmd, check=True)
    return dest


def workdir_for(src: Path) -> Path:
    digest = hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12]
    d = WORK_ROOT / f"{src.stem[:40]}_{digest}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def probe_duration(src: Path) -> float:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", str(src)], capture_output=True, text=True)
    return float(r.stdout.strip())


def detect_silences(src: Path, noise_db: float, min_gap: float):
    r = _run(["ffmpeg", "-i", str(src),
              "-af", f"silencedetect=noise={noise_db}dB:duration={min_gap}",
              "-f", "null", "-"], check=False, capture_output=True, text=True)
    sil, cur = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m: cur = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and cur is not None:
            sil.append((cur, float(m.group(1)))); cur = None
    return sil


def transcribe(src: Path, wd: Path):
    out = wd / "words.json"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return json.loads(out.read_text(encoding="utf-8"))
    _run([str(VENV_PY), str(TRANSCRIBE), str(src)], check=True)
    skill_wd = Path.home() / ".cache" / "video-edit"
    digest = hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12]
    sw = skill_wd / f"{src.stem[:40]}_{digest}" / "words.json"
    if sw.exists() and sw != out:
        shutil.copy(sw, out)
    return json.loads(out.read_text(encoding="utf-8"))


def find_retakes(words):
    if not words: return []
    from difflib import SequenceMatcher
    cuts = []
    # stutter
    for i in range(len(words) - 1):
        a = words[i]["word"].lower().strip(".,!?\"'")
        b = words[i+1]["word"].lower().strip(".,!?\"'")
        if a == b and (words[i+1]["start"] - words[i]["end"]) < 0.8 and len(a) >= 2:
            cuts.append((words[i]["start"] - 0.04, words[i]["end"] + 0.04))
    # sentence retakes
    sentences, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"]) > 0.4:
            sentences.append(cur); cur = []
        cur.append(w)
    if cur: sentences.append(cur)
    used = set()
    for i in range(len(sentences) - 1):
        if i in used: continue
        sa = sentences[i]
        a_tokens = [w["word"].lower().strip(".,!?\"'") for w in sa]
        if len(a_tokens) < 2: continue
        for j in range(i+1, min(i+4, len(sentences))):
            sb = sentences[j]
            if sb[0]["start"] - sa[-1]["end"] > 4.0: break
            b_tokens = [w["word"].lower().strip(".,!?\"'") for w in sb]
            if len(b_tokens) < 2: continue
            sm = SequenceMatcher(None, a_tokens, b_tokens, autojunk=False)
            lo = sm.find_longest_match(0, len(a_tokens), 0, len(b_tokens))
            ratio_a = lo.size / len(a_tokens)
            is_prefix = lo.a == 0 and lo.b == 0 and lo.size >= 3
            is_overlap = lo.size >= 3 and ratio_a >= 0.5
            if is_prefix or is_overlap:
                ms = sa[lo.a]; me = sa[lo.a + lo.size - 1]
                cuts.append((ms["start"] - 0.05, me["end"] + 0.05))
                used.add(i); break
    if not cuts: return []
    cuts.sort()
    merged = [list(cuts[0])]
    for s, e in cuts[1:]:
        if s <= merged[-1][1] + 0.1: merged[-1][1] = max(merged[-1][1], e)
        else: merged.append([s, e])
    return [tuple(m) for m in merged]


def build_keeps(total, silences, retake_cuts, target_gap, cut_head, cut_tail):
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
            prev = se; continue
        if is_tail:
            if ss > prev: keeps.append((prev, ss))
            prev = se; break
        if contains_retake:
            if ss > prev: keeps.append((prev, ss))
            prev = se; continue
        seg_end = ss + min(se - ss, target_gap)
        if seg_end > prev: keeps.append((prev, seg_end))
        prev = se
    if prev < total: keeps.append((prev, total))
    return keeps


def build_audio_chain(denoise_i: float, enhance_i: float) -> str:
    """Compose ffmpeg audio filter chain. Each intensity 0-1.
    denoise: RNNoise + spectral afftdn. enhance: EQ + compress + loudness norm."""
    filters = []
    if denoise_i > 0.02:
        nf = -20 - int(15 * denoise_i)  # -20 to -35
        filters.append(f"afftdn=nf={nf}:nt=w")
    if enhance_i > 0.02:
        cut_db = int(-3 - 6 * enhance_i)  # -3 to -9
        boost_db = round(2 + 3 * enhance_i, 1)  # +2 to +5
        ratio = round(2 + 2 * enhance_i, 1)  # 2 to 4
        filters.append(f"equalizer=f=80:t=h:g={cut_db}")
        filters.append(f"equalizer=f=4000:t=q:w=2:g={boost_db}")
        filters.append(f"acompressor=threshold=-18dB:ratio={ratio}:attack=5:release=80")
        filters.append("dynaudnorm=p=0.95:m=10:s=12")
    return ",".join(filters) if filters else "anull"


def splice(src, keeps, out, audio_filter: str = "anull"):
    parts, labels = [], []
    for i, (a, b) in enumerate(keeps):
        parts.append(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(keeps)}:v=1:a=1[outv][outaraw]")
    if audio_filter and audio_filter != "anull":
        parts.append(f"[outaraw]{audio_filter}[outa]")
        out_a_map = "[outa]"
    else:
        out_a_map = "[outaraw]"
    _run(["ffmpeg", "-y", "-i", str(src), "-filter_complex", ";".join(parts),
          "-map", "[outv]", "-map", out_a_map,
          "-c:v", "libx264", "-preset", "fast", "-crf", "20",
          "-c:a", "aac", "-b:a", "192k", str(out)], check=True)


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


def detect_clicks(src: Path) -> list[tuple[float, float]]:
    """Detect short pops/clicks: very short loud transients (silencedetect inverse).
    Heuristic: silences at very tight threshold pick out non-silence segments,
    short ones (< 0.15s) flanked by silence on both sides = likely click."""
    sil = detect_silences(src, -20, 0.05)
    clicks = []
    for i in range(len(sil) - 1):
        gap_start = sil[i][1]
        gap_end = sil[i+1][0]
        if 0 < gap_end - gap_start < 0.15:
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


def copy_to_render_wd(src, edit_wd):
    """render.sh on Windows hashes the path differently. Copy plan to likely render workdirs."""
    bp = "/" + str(src).replace("\\", "/").replace(":", "")
    if bp[1].isalpha(): bp = "/" + bp[1].lower() + bp[2:]
    digests = [
        hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12],
        hashlib.sha1(bp.encode()).hexdigest()[:12],
        hashlib.sha1(str(src).replace("\\", "/").encode()).hexdigest()[:12],
    ]
    for d in set(digests):
        tgt = WORK_ROOT / f"{src.stem[:40]}_{d}"
        if tgt == edit_wd: continue
        tgt.mkdir(parents=True, exist_ok=True)
        for f in ("broll_plan.json", "words.json"):
            if (edit_wd / f).exists(): shutil.copy(edit_wd / f, tgt / f)
        if (edit_wd / "broll").exists():
            tb = tgt / "broll"; tb.mkdir(exist_ok=True)
            for f in (edit_wd / "broll").iterdir(): shutil.copy(f, tb)
        (tgt / ".polished").touch()
        (tgt / "broll_plan.source.json").unlink(missing_ok=True)


def append_chat(sid, role, text, typ="msg"):
    sess = SESSIONS[sid]
    sess.setdefault("chat", []).append({"role": role, "text": text, "type": typ, "ts": time.time()})


# ───────────── Jobs ─────────────
def job_clean(sid):
    j = SESSIONS[sid]
    # If multiple sources provided, combine first
    sources = j.get("sources") or [j["src"]]
    if len(sources) > 1:
        j["status"] = "clean:combining"
        first = Path(sources[0])
        combined = first.parent / f"{first.stem}.combined.mp4"
        try:
            combine_clips(sources, combined)
            j["src"] = str(combined)
            j["combined_path"] = str(combined)
        except Exception as e:
            import traceback
            j["status"] = "clean:error"; j["error"] = f"combine failed: {e}"
            j["trace"] = traceback.format_exc()
            log_error(sid, "combine", str(e))
            return
    j["status"] = "clean:transcribing"
    src = Path(j["src"])
    wd = workdir_for(src); j["workdir"] = str(wd)
    try:
        words = transcribe(src, wd)
        j["words"] = words  # expose for transcript editor
        j["status"] = "clean:analyzing"
        silences = detect_silences(src, j["noise_db"], j["min_gap"])
        retake_cuts = find_retakes(words) if j["remove_retakes"] else []
        click_cuts = detect_clicks(src) if j.get("remove_clicks") else []
        manual_cuts = j.get("manual_cuts", [])  # from transcript editor
        extra_cuts = retake_cuts + click_cuts + manual_cuts
        j["status"] = "clean:splicing"
        total = probe_duration(src)
        keeps = build_keeps(total, silences, extra_cuts, j["target_gap"], j["cut_head"], j["cut_tail"])
        # snapshot for undo
        j.setdefault("history", []).append({
            "manual_cuts": list(manual_cuts),
            "noise_db": j["noise_db"], "min_gap": j["min_gap"], "target_gap": j["target_gap"],
            "cut_head": j["cut_head"], "cut_tail": j["cut_tail"],
            "remove_retakes": j["remove_retakes"], "remove_clicks": j.get("remove_clicks", False),
            "denoise_intensity": j.get("denoise_intensity", 0),
            "enhance_intensity": j.get("enhance_intensity", 0),
        })
        if len(j["history"]) > 20: j["history"] = j["history"][-20:]

        audio_filter = build_audio_chain(j.get("denoise_intensity", 0), j.get("enhance_intensity", 0))
        out = src.parent / f"{src.stem}.clean.mp4"
        splice(src, keeps, out, audio_filter=audio_filter)
        j["clean_path"] = str(out)
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
        words = transcribe(src, wd)
        plan = generate_plan(words)
        (wd / "broll_plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=True), encoding="ascii")
        j["plan"] = plan
        append_chat(sid, "assistant", f"**Plan generated** — {len(plan)} beats: " + ", ".join(b['kind'] for b in plan), "plan")
        copy_to_render_wd(src, wd)
        j["status"] = "edit:rendering"
        append_chat(sid, "assistant", "Rendering preview (~30s)...", "status")
        # clear stale locks
        for p in Path("/tmp").glob("video-edit-render.lock*"):
            try:
                if p.is_dir(): shutil.rmtree(p, ignore_errors=True)
                else: p.unlink(missing_ok=True)
            except Exception: pass
        env = {"FORCE_RENDER": "1", **dict(os.environ)}
        r = subprocess.run(["bash", str(RENDER_SH), str(src)], env=env, cwd=str(SKILL),
                           capture_output=True, text=True)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "")[-600:]
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
        env = {"QUALITY": "final", "FORCE_RENDER": "1", **dict(os.environ)}
        _run(["bash", str(RENDER_SH), str(src)], check=True, env=env, cwd=str(SKILL))
        final = src.parent / f"{src.stem}.enhanced.mp4"
        if final.exists():
            j["final_path"] = str(final); j["status"] = "export:done"
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
    wd = Path(sess.get("edit_workdir") or sess.get("workdir") or "")
    return wd / "broll_plan.json" if wd else None


def load_plan(sid: str) -> list:
    p = get_plan_path(sid)
    if p and p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except: return []
    return []


def save_plan(sid: str, plan: list, push_history: bool = True):
    p = get_plan_path(sid)
    if not p: return
    if push_history:
        cur = load_plan(sid)
        PLAN_HISTORY.setdefault(sid, []).append(cur)
        if len(PLAN_HISTORY[sid]) > 30:
            PLAN_HISTORY[sid] = PLAN_HISTORY[sid][-30:]
        PLAN_FUTURE[sid] = []
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan, indent=2, ensure_ascii=True), encoding="ascii")
    # mirror to render.sh's workdir variants
    sess = SESSIONS.get(sid)
    if sess and sess.get("clean_path"):
        copy_to_render_wd(Path(sess["clean_path"]).resolve(), p.parent)


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
    wd = Path(sess.get("edit_workdir") or sess.get("workdir") or "")
    if not wd: return None
    broll = wd / "broll"; broll.mkdir(exist_ok=True)
    import urllib.request
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)[:40] + ".jpg"
    dest = broll / safe
    try:
        urllib.request.urlretrieve(url, dest)
        return f"broll/{safe}"
    except Exception:
        return None


def launch_studio():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 3001)); s.close()
        subprocess.Popen(["npx", "--no-install", "remotion", "studio", "src/index.ts", "--port", "3001"],
                         cwd=str(SKILL / "remotion"))
        return True
    except OSError:
        s.close(); return False


ERROR_LOG: list[dict] = []  # recent errors across all sessions

def log_error(sid: str, where: str, msg: str):
    ERROR_LOG.append({"ts": time.time(), "sid": sid, "where": where, "msg": str(msg)[:500]})
    if len(ERROR_LOG) > 100: del ERROR_LOG[:-100]


def save_project(sid: str) -> Path | None:
    sess = SESSIONS.get(sid)
    if not sess: return None
    src = Path(sess.get("src") or sess.get("clean_path") or "")
    if not src.exists(): return None
    wd = Path(sess.get("workdir") or workdir_for(src))
    proj = wd / "project.studio.json"
    snapshot = {k: v for k, v in sess.items()
                if k not in ("waveform", "words")}  # heavy, derivable
    snapshot["saved_at"] = time.time()
    snapshot["src"] = str(src)
    snapshot["thumbnail_at"] = 1.0  # extract a frame at 1s for thumbnail
    proj.write_text(json.dumps(snapshot, indent=2, ensure_ascii=True), encoding="ascii")
    # extract a small thumbnail jpg
    thumb = wd / "thumb.jpg"
    try:
        subprocess.run(["ffmpeg", "-y", "-ss", "1", "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=320:-1", str(thumb)],
                       check=False, capture_output=True, timeout=15)
    except Exception: pass
    return proj


def load_project(proj_path: Path) -> str | None:
    """Load a saved project, return new sid."""
    if not proj_path.exists(): return None
    snap = json.loads(proj_path.read_text(encoding="utf-8"))
    sid = uuid.uuid4().hex
    SESSIONS[sid] = snap
    # re-derive transcript if needed
    src = Path(snap.get("src", ""))
    wd = Path(snap.get("workdir") or workdir_for(src))
    if (wd / "words.json").exists():
        try: SESSIONS[sid]["words"] = json.loads((wd / "words.json").read_text(encoding="utf-8"))
        except: pass
    return sid


def list_projects() -> list[dict]:
    out = []
    if not WORK_ROOT.exists(): return out
    for proj in WORK_ROOT.glob("*/project.studio.json"):
        try:
            d = json.loads(proj.read_text(encoding="utf-8"))
            src = d.get("src", "")
            out.append({
                "path": str(proj),
                "name": Path(src).name if src else proj.parent.name,
                "src": src,
                "saved_at": d.get("saved_at", 0),
                "status": d.get("status", "?"),
                "thumb": str(proj.parent / "thumb.jpg") if (proj.parent / "thumb.jpg").exists() else None,
            })
        except Exception:
            pass
    out.sort(key=lambda p: -p.get("saved_at", 0))
    return out


def _just_render(sid: str):
    """Re-render preview only (no plan regen)."""
    sess = SESSIONS.get(sid)
    if not sess: return
    sess["status"] = "edit:rendering"
    src = Path(sess.get("clean_path") or sess["src"]).resolve()
    wd = Path(sess.get("edit_workdir") or sess.get("workdir") or "")
    if wd: copy_to_render_wd(src, wd)
    # clear any stale render lock
    for p in Path("/tmp").glob("video-edit-render.lock*"):
        try:
            if p.is_dir(): shutil.rmtree(p, ignore_errors=True)
            else: p.unlink(missing_ok=True)
        except Exception: pass
    try:
        env = {"FORCE_RENDER": "1", **dict(os.environ)}
        r = subprocess.run(["bash", str(RENDER_SH), str(src)], env=env, cwd=str(SKILL),
                           capture_output=True, text=True)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "")[-800:]
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


def launch_tuner():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 5050)); s.close()
        subprocess.Popen(["python3", str(SKILL / "scripts/position_tuner.py")])
        return True
    except OSError:
        s.close(); return False


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
.player{width:100%;border-radius:8px;background:#000;margin-top:12px}
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
      <div class="toggle on" data-key="cut_tail"><div class="lbl">Cut tail silence<small>Trim dead air at end</small></div><div class="switch"></div></div>
      <div class="toggle on" data-key="remove_retakes"><div class="lbl">Remove retakes<small>Detect repeated phrases, keep last take</small></div><div class="switch"></div></div>
      <div class="toggle" data-key="remove_clicks"><div class="lbl">Remove clicks &amp; pops<small>Short transients between silences</small></div><div class="switch"></div></div>
    </div>
  </div>
  <div class="card">
    <h2>Audio enhancement</h2>
    <div class="row"><label>Background noise removal<small>RNNoise + spectral denoise. 0 = off</small></label>
      <input type="range" id="denoise" min="0" max="1" step="0.05" value="0" oninput="document.getElementById('n-denoise').value=this.value">
      <input type="number" id="n-denoise" value="0" min="0" max="1" step="0.05" oninput="document.getElementById('denoise').value=this.value"></div>
    <div class="row"><label>Studio sound enhance<small>EQ + compress + loudness norm. 0 = off</small></label>
      <input type="range" id="enhance" min="0" max="1" step="0.05" value="0" oninput="document.getElementById('n-enhance').value=this.value">
      <input type="number" id="n-enhance" value="0" min="0" max="1" step="0.05" oninput="document.getElementById('enhance').value=this.value"></div>
  </div>
  <div class="card hidden" id="wf-card">
    <h2>Waveform <span style="color:#7a8497;font-size:9px;font-weight:400;text-transform:none;letter-spacing:0;margin-left:8px">red = silences cut</span></h2>
    <canvas id="wf" style="width:100%;height:120px;background:#0F121A;border-radius:6px;display:block"></canvas>
  </div>
  <div class="card hidden" id="tr-card">
    <h2>Transcript editor <span style="color:#7a8497;font-size:9px;font-weight:400;text-transform:none;letter-spacing:0;margin-left:8px">click word to delete &middot; click again to restore</span></h2>
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
        <li><strong>Remotion Studio (:3001)</strong> — live preview of rendered comp w/ HMR on template edits</li>
        <li><strong>Position Tuner (:5050)</strong> — slider-based placement tuning for every overlay</li>
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

function tab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelector('.tab[data-tab='+name+']').classList.add('active');
  ['dash','clean','edit','export'].forEach(n=>$('t-'+n).classList.add('hidden'));
  $('t-'+name).classList.remove('hidden');
  if(name==='dash') loadProjects();
}
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
      <div style="font-size:12px;color:var(--text);font-weight:600;text-overflow:ellipsis;overflow:hidden;white-space:nowrap" title="${esc(p.src)}">${esc(p.name)}</div>
      <div style="font-size:10px;color:var(--dim);margin-top:4px">${new Date(p.saved_at*1000).toLocaleString()} · ${p.status}</div>`;
    div.onclick = ()=>openProject(p.path);
    grid.appendChild(div);
  });
}
async function openProject(path){
  const r = await fetch('/project/open',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({path})});
  const j = await r.json();
  if(j.sid){ sid = j.sid; toast('Project loaded'); tab('clean'); pollClean(); loadPlan(); loadMusic(); }
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

function gatherCleanParams(){
  let sources = clipQueue.length ? clipQueue.slice() : [];
  if(!sources.length){
    const p = pathInput.value.trim();
    if(p && !p.startsWith('(')) sources = [p];
  }
  const params = { sources, src: sources[0]||'', noise_db:+$('noise').value, min_gap:+$('mingap').value, target_gap:+$('target').value,
    denoise_intensity:+$('denoise').value, enhance_intensity:+$('enhance').value };
  ['cut_head','cut_tail','remove_retakes','remove_clicks'].forEach(k=>{
    params[k] = document.querySelector('.toggle[data-key='+k+']').classList.contains('on');
  });
  return params;
}
async function runClean(){
  const params = gatherCleanParams();
  if(!params.sources.length){ alert('Drop at least one video'); return; }
  $('b-clean').disabled=true; $('b-clean').textContent='Processing...';
  if(sid){
    // re-clean with new knobs (preserve manual_cuts)
    await fetch('/clean/reclean', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid, ...params})});
    pollClean(); return;
  }
  const r = await fetch('/clean/start', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(params)});
  const j = await r.json();
  if(j.error){ showCleanError(j.error); return; }
  sid = j.sid; pollClean();
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
    span.addEventListener('click', async ()=>{
      if(isCut(w)){
        // find idx of cut covering this word
        const idx = cuts.findIndex(c => c[0] <= w.start && w.end <= c[1]);
        await fetch('/transcript/restore', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid, idx})});
      } else {
        await fetch('/transcript/cut', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid, start:w.start - 0.04, end:w.end + 0.04})});
      }
      loadTranscript();
    });
    div.appendChild(span);
  });
  $('tr-cuts').textContent = cuts.length ? `${cuts.length} manual cut(s) — click Clean video to apply` : 'No manual cuts. Click any word above to mark for removal.';
}
async function pollClean(){
  const r = await fetch('/state?sid='+sid); const j = await r.json();
  renderClean(j);
  if(j.status==='clean:done' || j.status==='clean:error'){
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
      <a class="dl" href="/file?p=${encodeURIComponent(j.clean_path)}" download>Download clean.mp4</a>
      <button class="btn secondary" style="margin-top:10px" onclick="tab('edit')">Continue to Edit →</button></div>`;
  } else if(j.status==='clean:error'){
    div.innerHTML = `<div class="card"><div class="status"><span class="err">ERROR</span> ${j.error||''}</div></div>`;
  } else {
    const label = j.status==='clean:combining' ? `⚡ Combining ${(j.sources||[]).length} clips...` : `⚡ ${j.status}`;
    div.innerHTML = `<div class="card"><div class="status"><span class="ok">${label}</span></div></div>`;
  }
}
function showCleanError(m){ $('clean-result').classList.remove('hidden'); $('clean-result').innerHTML=`<div class="card"><div class="status"><span class="err">${m}</span></div></div>`; $('b-clean').disabled=false; $('b-clean').textContent='Try again'; }

async function runEdit(){
  const p = $('edit-path').value.trim(); if(!p){ alert('Need a clean video path'); return; }
  $('b-edit').disabled=true; $('b-edit').textContent='Editing...';
  if(!sid){
    const r = await fetch('/clean/start', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({src:p, cut_head:false, cut_tail:false, remove_retakes:false, noise_db:-32, min_gap:99, target_gap:0.3})});
    const j = await r.json(); sid = j.sid;
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
  subscribe:'#ef4444', portrait_burst:'#a78bfa', ratio_dots:'#60a5fa'
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
    if(edge==='start') currentPlan[idx].start_sec = +t.toFixed(2);
    else currentPlan[idx].end_sec = +t.toFixed(2);
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
    {label:'kind', html:`<select data-field="kind">${['hook_title','word_pop','stat_punch','quote_pull','image_card','tool_logo_burst','bar_overlay','bullet_burst','ratio_dots','subscribe','portrait_burst'].map(k=>`<option ${b.kind===k?'selected':''} value="${k}">${k}</option>`).join('')}</select>`},
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
  }
  return out;
}

async function updateField(field, value){
  if(selectedIdx<0) return;
  let v = value;
  if(field==='start_sec' || field==='end_sec' || field==='vertical' || field==='card_top' || field==='total' || field==='marked') v = +value;
  if(field==='items' || field==='bars'){
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
  const r = await fetch('/state?sid='+sid); const j = await r.json();
  renderEdit(j);
  if(j.chat) renderChat(j.chat);
  if(j.status==='edit:done' || j.status==='edit:error'){
    $('b-edit').disabled=false; $('b-edit').textContent='Re-edit';
    return;
  }
  setTimeout(pollEdit, 1500);
}
function renderEdit(j){
  const div = $('edit-result');
  if(j.status==='edit:done' && j.preview_path){
    div.classList.remove('hidden');
    div.innerHTML = `<div class="card"><h2>Preview</h2>
      <video class="player" controls src="/file?p=${encodeURIComponent(j.preview_path)}&t=${Date.now()}"></video>
    </div>`;
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
async function openTools(){
  await fetch('/tools/studio', {method:'POST'});
  await fetch('/tools/tuner', {method:'POST'});
  window.open('http://localhost:3001','_blank');
  setTimeout(()=>window.open('http://localhost:5050','_blank'), 500);
}

async function runFinal(){
  if(!sid){ alert('Run Edit first'); return; }
  if(!confirm('Render final at '+preset+' quality?')) return;
  $('b-final').disabled=true; $('b-final').textContent='Rendering...';
  await fetch('/export/start', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({sid, preset})});
  pollFinal();
}
async function pollFinal(){
  const r = await fetch('/state?sid='+sid); const j = await r.json();
  const div=$('export-result'); div.classList.remove('hidden');
  if(j.status==='export:done' && j.final_path){
    div.innerHTML = `<div class="card"><h2>🎬 Final ready</h2>
      <video class="player" controls src="/file?p=${encodeURIComponent(j.final_path)}&t=${Date.now()}"></video>
      <a class="dl" href="/file?p=${encodeURIComponent(j.final_path)}" download>Download final .mp4</a></div>`;
    $('b-final').disabled=false; $('b-final').textContent='Re-render';
    return;
  }
  if(j.status==='export:error'){
    div.innerHTML = `<div class="card"><div class="status"><span class="err">${j.error}</span></div></div>`;
    $('b-final').disabled=false; $('b-final').textContent='Try again';
    return;
  }
  div.innerHTML = `<div class="card"><div class="status"><span class="ok">⚡ ${j.status}</span></div></div>`;
  setTimeout(pollFinal, 2000);
}
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
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
            p = Path(parse_qs(u.query).get("p", [""])[0])
            if not p.exists(): self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(p.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
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
            SESSIONS[sid] = {
                "src": str(src.resolve()),
                "sources": resolved,
                "noise_db": body.get("noise_db", -32),
                "min_gap": body.get("min_gap", 0.30),
                "target_gap": body.get("target_gap", 0.30),
                "cut_head": body.get("cut_head", True),
                "cut_tail": body.get("cut_tail", True),
                "remove_retakes": body.get("remove_retakes", True),
                "remove_clicks": body.get("remove_clicks", False),
                "denoise_intensity": body.get("denoise_intensity", 0.0),
                "enhance_intensity": body.get("enhance_intensity", 0.0),
                "manual_cuts": body.get("manual_cuts", []),
                "status": "clean:queued", "chat": [], "history": [],
            }
            threading.Thread(target=job_clean, args=(sid,), daemon=True).start()
            self._json(200, {"sid": sid}); return

        if u.path == "/clean/reclean":
            sid = body.get("sid")
            if sid not in SESSIONS: self._json(404, {"error": "no session"}); return
            sess = SESSIONS[sid]
            # update knobs from body
            for k in ("noise_db", "min_gap", "target_gap", "cut_head", "cut_tail",
                      "remove_retakes", "remove_clicks", "denoise_intensity", "enhance_intensity"):
                if k in body: sess[k] = body[k]
            if "manual_cuts" in body: sess["manual_cuts"] = body["manual_cuts"]
            threading.Thread(target=job_clean, args=(sid,), daemon=True).start()
            self._json(200, {"ok": True}); return

        if u.path == "/clean/undo":
            sid = body.get("sid")
            sess = SESSIONS.get(sid)
            if not sess or len(sess.get("history", [])) < 2:
                self._json(400, {"error": "nothing to undo"}); return
            sess["history"].pop()  # drop current
            prev = sess["history"][-1]
            for k, v in prev.items(): sess[k] = v
            threading.Thread(target=job_clean, args=(sid,), daemon=True).start()
            self._json(200, {"ok": True}); return

        if u.path == "/transcript/cut":
            sid = body.get("sid")
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            # body: {"start": float, "end": float}
            mc = sess.setdefault("manual_cuts", [])
            mc.append([body["start"], body["end"]])
            self._json(200, {"manual_cuts": mc}); return

        if u.path == "/transcript/restore":
            sid = body.get("sid")
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            idx = body.get("idx", -1)
            mc = sess.get("manual_cuts", [])
            if 0 <= idx < len(mc): mc.pop(idx)
            self._json(200, {"manual_cuts": mc}); return

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
            append_chat(sid, "assistant", f"Note added. Click **Auto-edit** to regen w/ this feedback.", "msg")
            self._json(200, {"ok": True}); return

        if u.path == "/export/start":
            sid = body.get("sid")
            if sid not in SESSIONS: self._json(404, {"error": "no session"}); return
            threading.Thread(target=job_final, args=(sid,), daemon=True).start()
            self._json(200, {"ok": True}); return

        if u.path == "/tools/studio":
            launch_studio(); self._json(200, {"ok": True}); return
        if u.path == "/tools/tuner":
            launch_tuner(); self._json(200, {"ok": True}); return

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
                if edge == "start": plan[idx]["start_sec"] = round(new_t, 2)
                else: plan[idx]["end_sec"] = round(new_t, 2)
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

        if u.path == "/asset/upload":
            # multipart upload handled inline (basic parser)
            sid = parse_qs(urlparse(self.path).query).get("sid", [""])[0]
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            # only support simple raw-body upload w/ filename header
            fname = self.headers.get("X-Filename", f"upload_{int(time.time())}.bin")
            wd = Path(sess.get("edit_workdir") or sess.get("workdir") or "")
            broll = wd / "broll"; broll.mkdir(parents=True, exist_ok=True)
            n = int(self.headers.get("Content-Length", 0))
            (broll / fname).write_bytes(self.rfile.read(n))
            self._json(200, {"image_path": f"broll/{fname}"}); return

        self.send_response(404); self.end_headers()


if __name__ == "__main__":
    port = int(os.getenv("STUDIO_PORT", "5056"))  # 5000 is taken by macOS AirPlay Receiver
    print(f"Video Studio: http://localhost:{port}")
    HTTPServer(("localhost", port), H).serve_forever()
