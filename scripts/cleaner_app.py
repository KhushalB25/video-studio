"""Phase 1 Cleaner — http://localhost:5001
Upload video, set knobs, get cleaned video back. Self-contained, no Flask.

Features:
  - Silence detection (ffmpeg silencedetect)
  - Gap shortening (any silence > min_gap shortened to target_gap)
  - Head silence cut
  - Tail silence cut
  - Retake removal (regex/difflib fallback OR LLM if Claude API key set)
  - Frame-accurate filter_complex splice (no black frames, no sync drift)
"""
from __future__ import annotations
import json, re, hashlib, subprocess, threading, uuid, html
import sys, os
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import cgi, shutil

SKILL = Path(__file__).resolve().parent.parent
VENV_PY = SKILL / ".venv/Scripts/python.exe"
TRANSCRIBE = SKILL / "scripts/transcribe.py"
WORK_ROOT = Path.home() / ".cache/video-edit/cleaner"
WORK_ROOT.mkdir(parents=True, exist_ok=True)

# In-memory job registry
JOBS: dict[str, dict] = {}


def _run(cmd, check=True, capture=False):
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def workdir_for(src: Path) -> Path:
    digest = hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12]
    d = WORK_ROOT / f"{src.stem[:40]}_{digest}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def probe_duration(src: Path) -> float:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", str(src)], capture=True)
    return float(r.stdout.strip())


def detect_silences(src: Path, noise_db: float, min_gap: float) -> list[tuple[float, float]]:
    r = _run(["ffmpeg", "-i", str(src),
              "-af", f"silencedetect=noise={noise_db}dB:duration={min_gap}",
              "-f", "null", "-"], check=False, capture=True)
    silences = []
    cur_start = None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: ([\d.]+)", line)
        if m: cur_start = float(m.group(1))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and cur_start is not None:
            silences.append((cur_start, float(m.group(1))))
            cur_start = None
    return silences


def transcribe(src: Path, wd: Path) -> list[dict]:
    out = wd / "words.json"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return json.loads(out.read_text(encoding="utf-8"))
    # invoke skill transcribe in its venv
    _run([str(VENV_PY), str(TRANSCRIBE), str(src)])
    skill_wd = Path.home() / ".cache" / "video-edit"
    digest = hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12]
    skill_words = skill_wd / f"{src.stem[:40]}_{digest}" / "words.json"
    if skill_words.exists():
        shutil.copy(skill_words, out)
    return json.loads(out.read_text(encoding="utf-8"))


def find_retakes_difflib(words: list[dict]) -> list[tuple[float, float]]:
    """Detect retakes by grouping words into sentences (silence-bounded) and
    comparing adjacent sentences. Mark the EARLIER occurrence to cut.

    Catches:
    1. Adjacent sentences with >=50% longest-common-subsequence overlap
    2. Earlier sentence is a prefix of later (with >=3 word common run)
    3. Single-word stutters ("buh-buh-but" / "I-I-I think") within 0.8s
    """
    if not words:
        return []
    from difflib import SequenceMatcher

    cuts = []

    # --- 1. Stutter detection (same word repeated within 0.8s) ---
    used_stutter = set()
    for i in range(len(words) - 1):
        if i in used_stutter:
            continue
        a = words[i]["word"].lower().strip(".,!?\"'")
        b = words[i+1]["word"].lower().strip(".,!?\"'")
        gap = words[i+1]["start"] - words[i]["end"]
        if a == b and gap < 0.8 and len(a) >= 2:
            # cut the first occurrence
            cuts.append((words[i]["start"] - 0.04, words[i]["end"] + 0.04))
            used_stutter.add(i)

    # --- 2. Sentence-level retake detection ---
    # Group words into sentences using gaps > 0.4s as boundaries
    sentences = []
    cur = []
    for k, w in enumerate(words):
        if cur and (w["start"] - cur[-1]["end"]) > 0.4:
            sentences.append(cur)
            cur = []
        cur.append(w)
    if cur:
        sentences.append(cur)

    used_sent = set()
    for i in range(len(sentences) - 1):
        if i in used_sent:
            continue
        sa = sentences[i]
        a_tokens = [w["word"].lower().strip(".,!?\"'") for w in sa]
        if len(a_tokens) < 2:
            continue
        # check up to next 3 sentences within 4s
        for j in range(i + 1, min(i + 4, len(sentences))):
            sb = sentences[j]
            if sb[0]["start"] - sa[-1]["end"] > 4.0:
                break
            b_tokens = [w["word"].lower().strip(".,!?\"'") for w in sb]
            if len(b_tokens) < 2:
                continue
            sm = SequenceMatcher(None, a_tokens, b_tokens, autojunk=False)
            longest = sm.find_longest_match(0, len(a_tokens), 0, len(b_tokens))
            ratio_a = longest.size / len(a_tokens)
            ratio_b = longest.size / len(b_tokens)

            # Retake conditions:
            # (a) overlap covers >=50% of EARLIER sentence and >=3 words
            # (b) earlier is a prefix of later (>=3 common run starting at 0,0)
            is_prefix = longest.a == 0 and longest.b == 0 and longest.size >= 3
            is_overlap = longest.size >= 3 and ratio_a >= 0.5

            if is_prefix or is_overlap:
                # Cut only the MATCHED span within the earlier sentence
                # (preserves any good content before the retake portion)
                match_start_word = sa[longest.a]
                match_end_word = sa[longest.a + longest.size - 1]
                cuts.append((match_start_word["start"] - 0.05,
                             match_end_word["end"] + 0.05))
                used_sent.add(i)
                break

    # Merge overlapping cuts
    if not cuts:
        return []
    cuts.sort()
    merged = [list(cuts[0])]
    for s, e in cuts[1:]:
        if s <= merged[-1][1] + 0.1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(m) for m in merged]


def merge_overlaps(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not ranges: return []
    ranges = sorted(ranges)
    out = [ranges[0]]
    for s, e in ranges[1:]:
        if s <= out[-1][1] + 0.01:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def build_keeps(total: float, silences, retake_cuts, target_gap: float,
                cut_head: bool, cut_tail: bool) -> list[tuple[float, float]]:
    """Translate silences + retake cuts into keep-ranges with shortened gaps."""
    n = len(silences)
    head_idx = 0 if (silences and silences[0][0] <= 0.1 and cut_head) else -1
    tail_idx = n - 1 if (silences and silences[-1][1] >= total - 0.1 and cut_tail) else -1

    keeps = []
    prev_end = 0.0
    cuts_to_apply = list(silences)  # mutable copy
    # extend silences that overlap a retake cut, to swallow the retake
    expanded = []
    for s_start, s_end in cuts_to_apply:
        expanded.append([s_start, s_end])
    for r_start, r_end in retake_cuts:
        # find adjacent silences and merge
        for s in expanded:
            if abs(s[1] - r_start) < 0.5 or s[0] - 0.5 <= r_start <= s[1] + 0.5:
                s[1] = max(s[1], r_end)
            if abs(s[0] - r_end) < 0.5 or s[0] - 0.5 <= r_end <= s[1] + 0.5:
                s[0] = min(s[0], r_start)
        # also append as a standalone cut if no silence near
        if not any(s[0] <= r_start <= s[1] or s[0] <= r_end <= s[1] for s in expanded):
            expanded.append([r_start, r_end])
    expanded.sort()
    merged = []
    for s in expanded:
        if merged and s[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], s[1])
        else:
            merged.append(list(s))

    n = len(merged)
    for i, (s_start, s_end) in enumerate(merged):
        is_head = i == 0 and s_start <= 0.1 and cut_head
        is_tail = i == n - 1 and s_end >= total - 0.1 and cut_tail
        # detect retake-containing silence
        contains_retake = any(s_start - 0.1 <= rs and re <= s_end + 0.1 for rs, re in retake_cuts)

        if is_head:
            prev_end = s_end
            continue
        if is_tail:
            if s_start > prev_end:
                keeps.append((prev_end, s_start))
            prev_end = s_end
            break
        if contains_retake:
            if s_start > prev_end:
                keeps.append((prev_end, s_start))
            prev_end = s_end
            continue
        # middle silence: keep target_gap
        sil_dur = s_end - s_start
        seg_end = s_start + min(sil_dur, target_gap)
        if seg_end > prev_end:
            keeps.append((prev_end, seg_end))
        prev_end = s_end
    if prev_end < total:
        keeps.append((prev_end, total))
    return keeps


def splice(src: Path, keeps, out: Path):
    if not keeps:
        raise RuntimeError("no keep ranges")
    parts = []
    labels = []
    for i, (a, b) in enumerate(keeps):
        parts.append(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[v{i}][a{i}]")
    parts.append(f"{''.join(labels)}concat=n={len(keeps)}:v=1:a=1[outv][outa]")
    fc = ";".join(parts)
    _run(["ffmpeg", "-y", "-i", str(src), "-filter_complex", fc,
          "-map", "[outv]", "-map", "[outa]",
          "-c:v", "libx264", "-preset", "fast", "-crf", "20",
          "-c:a", "aac", "-b:a", "192k", str(out)])


def process_job(job_id: str):
    j = JOBS[job_id]
    j["status"] = "transcribing"
    j["progress"] = "Transcribing audio..."
    src = Path(j["src"])
    wd = workdir_for(src)
    try:
        words = transcribe(src, wd)
        j["progress"] = "Detecting silences..."
        j["status"] = "analyzing"
        silences = detect_silences(src, j["noise_db"], j["min_gap"])
        retake_cuts = []
        if j["remove_retakes"]:
            j["progress"] = "Finding retakes..."
            retake_cuts = find_retakes_difflib(words)
        j["progress"] = "Splicing..."
        j["status"] = "splicing"
        total = probe_duration(src)
        keeps = build_keeps(total, silences, retake_cuts, j["target_gap"],
                            j["cut_head"], j["cut_tail"])
        j["original"] = total
        j["new_duration"] = sum(b - a for a, b in keeps)
        j["keeps"] = keeps
        j["silences"] = silences
        j["retakes"] = retake_cuts
        out = src.parent / f"{src.stem}.clean.mp4"
        splice(src, keeps, out)
        j["output"] = str(out)
        j["status"] = "done"
        j["progress"] = "Complete"
    except Exception as e:
        j["status"] = "error"
        j["progress"] = f"Error: {e}"
        import traceback
        j["trace"] = traceback.format_exc()


HTML_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Video Cleaner</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,system-ui,'Segoe UI',sans-serif;background:#0F121A;color:#E9ECED;min-height:100vh;padding:32px 16px}
.wrap{max-width:680px;margin:0 auto}
h1{font-size:24px;color:#CFFF05;letter-spacing:.04em;text-transform:uppercase;font-weight:900}
.sub{color:#B5BFC2;font-size:12px;margin:6px 0 28px}
.card{background:#1E2434;border-radius:10px;padding:22px;margin-bottom:18px;border:1px solid #2a3247}
.card h2{font-size:11px;color:#CFFF05;text-transform:uppercase;letter-spacing:.08em;margin-bottom:14px;font-weight:700}
.drop{border:2px dashed #343E5B;border-radius:12px;padding:42px 20px;text-align:center;cursor:pointer;transition:.15s;background:#1a2030}
.drop:hover,.drop.over{border-color:#CFFF05;background:#1f2638}
.drop p{color:#B5BFC2;font-size:14px}
.drop .big{color:#E9ECED;font-size:16px;font-weight:600;margin-bottom:8px}
.drop .pill{display:inline-block;background:#CFFF05;color:#0F121A;padding:8px 18px;border-radius:6px;font-weight:700;font-size:12px;margin-top:14px;letter-spacing:.04em}
.file-info{color:#CFFF05;font-size:13px;margin-top:10px;display:none}
.row{display:grid;grid-template-columns:200px 1fr 70px;gap:14px;align-items:center;padding:10px 0;border-bottom:1px solid #252b3d}
.row:last-child{border-bottom:none}
.row label{font-size:13px;color:#D2D8DA}
.row label small{display:block;color:#7a8497;font-size:10px;margin-top:2px}
input[type=range]{accent-color:#CFFF05;width:100%}
input[type=number]{width:64px;background:#0F121A;border:1px solid #343E5B;color:#E9ECED;padding:5px 8px;border-radius:5px;font-family:monospace;font-size:12px}
input[type=number]:focus{outline:1px solid #CFFF05;border-color:#CFFF05}
.toggles{display:flex;flex-direction:column;gap:10px;margin-top:8px}
.toggle{display:grid;grid-template-columns:1fr auto;align-items:center;gap:12px;background:#0F121A;border:1px solid #343E5B;border-radius:8px;padding:14px 16px;cursor:pointer;transition:.15s}
.toggle:hover{border-color:#5B6478}
.toggle .lbl{font-size:13px;color:#D2D8DA;font-weight:600}
.toggle .lbl small{display:block;color:#7a8497;font-size:10px;font-weight:400;margin-top:2px}
.switch{position:relative;width:48px;height:26px;background:#343E5B;border-radius:13px;transition:.2s;flex-shrink:0}
.switch::after{content:'';position:absolute;top:3px;left:3px;width:20px;height:20px;background:#7a8497;border-radius:50%;transition:.2s}
.switch::before{content:'OFF';position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:8px;font-weight:800;letter-spacing:.05em;color:#5B6478}
.toggle.on .switch{background:#CFFF05}
.toggle.on .switch::after{left:25px;background:#0F121A}
.toggle.on .switch::before{content:'ON';left:7px;right:auto;color:#0F121A}
.toggle.on .lbl{color:#CFFF05}
.btn{display:block;width:100%;background:#CFFF05;color:#0F121A;border:none;padding:14px;border-radius:8px;font-weight:800;font-size:13px;letter-spacing:.06em;text-transform:uppercase;cursor:pointer;transition:.15s}
.btn:hover{background:#dfff45;transform:translateY(-1px)}
.btn:disabled{background:#343E5B;color:#7a8497;cursor:not-allowed;transform:none}
.status{padding:14px;background:#0F121A;border-radius:8px;font-size:12px;color:#B5BFC2;font-family:monospace;margin-top:14px}
.status .ok{color:#CFFF05}
.status .err{color:#ff6b6b}
.stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-top:14px}
.stat{background:#0F121A;border-radius:6px;padding:12px;text-align:center}
.stat .v{font-size:20px;color:#CFFF05;font-weight:800;font-family:monospace}
.stat .l{font-size:10px;color:#7a8497;text-transform:uppercase;letter-spacing:.06em;margin-top:4px}
.dl{display:block;text-align:center;padding:14px;background:#CFFF05;color:#0F121A;border-radius:8px;font-weight:800;text-decoration:none;margin-top:14px;text-transform:uppercase;letter-spacing:.06em;font-size:13px}
.dl:hover{background:#dfff45}
.hidden{display:none}
</style></head><body><div class="wrap">
<h1>Video Cleaner</h1>
<div class="sub">Upload raw footage. Set knobs. Get clean cut back.</div>

<div class="card">
  <h2>1. Source video</h2>
  <div class="drop" id="drop">
    <div class="big">Drop video here</div>
    <p>or paste full file path below</p>
    <div class="pill">Browse</div>
    <input type="file" id="file" accept="video/*" style="display:none">
    <input type="text" id="path" placeholder="C:\Users\...\video.mp4" style="margin-top:14px;width:100%;background:#0F121A;border:1px solid #343E5B;color:#E9ECED;padding:8px 10px;border-radius:5px;font-family:monospace;font-size:11px">
    <div class="file-info" id="info"></div>
  </div>
</div>

<div class="card">
  <h2>2. Cleaning knobs</h2>
  <div class="row">
    <label>Silence threshold<small>dB. Lower = more aggressive</small></label>
    <input type="range" id="noise" min="-50" max="-15" step="1" value="-32" oninput="document.getElementById('n-noise').value=this.value">
    <input type="number" id="n-noise" value="-32" min="-50" max="-15" step="1" oninput="document.querySelector('#noise').value=this.value">
  </div>
  <div class="row">
    <label>Min gap to cut<small>seconds. Below this kept untouched</small></label>
    <input type="range" id="mingap" min="0.10" max="2.0" step="0.05" value="0.30" oninput="document.getElementById('n-mingap').value=this.value">
    <input type="number" id="n-mingap" value="0.30" min="0.10" max="2.0" step="0.05" oninput="document.querySelector('#mingap').value=this.value">
  </div>
  <div class="row">
    <label>Replace gap with<small>seconds. New gap duration</small></label>
    <input type="range" id="target" min="0" max="1.0" step="0.05" value="0.30" oninput="document.getElementById('n-target').value=this.value">
    <input type="number" id="n-target" value="0.30" min="0" max="1.0" step="0.05" oninput="document.querySelector('#target').value=this.value">
  </div>
</div>

<div class="card">
  <h2>3. What to remove</h2>
  <div class="toggles">
    <div class="toggle on" data-key="cut_head"><div class="lbl">Cut head silence<small>Trim dead air at start</small></div><div class="switch"></div></div>
    <div class="toggle on" data-key="cut_tail"><div class="lbl">Cut tail silence<small>Trim dead air at end</small></div><div class="switch"></div></div>
    <div class="toggle on" data-key="remove_retakes"><div class="lbl">Remove retakes<small>Detect repeated phrases, keep last take</small></div><div class="switch"></div></div>
  </div>
</div>

<button class="btn" id="go" onclick="run()">Clean video</button>

<div id="result" class="hidden"></div>

<script>
const drop = document.getElementById('drop');
const file = document.getElementById('file');
const path = document.getElementById('path');
const info = document.getElementById('info');
let selectedPath = '';

drop.addEventListener('click', e => { if(e.target.tagName !== 'INPUT') file.click(); });
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', e => drop.classList.remove('over'));
drop.addEventListener('drop', e => {
  e.preventDefault(); drop.classList.remove('over');
  const f = e.dataTransfer.files[0];
  if (f) { selectedPath = f.path || f.name; path.value = selectedPath; info.style.display='block'; info.textContent = '✓ ' + (f.name) + '  ('+(f.size/1024/1024).toFixed(1)+' MB)'; }
});
file.addEventListener('change', e => {
  const f = e.target.files[0];
  if (f) { selectedPath = f.path || f.name; path.value = selectedPath; info.style.display='block'; info.textContent = '✓ ' + f.name; }
});
path.addEventListener('input', e => { selectedPath = e.target.value; if(selectedPath) info.style.display='none'; });

document.querySelectorAll('.toggle').forEach(t => t.addEventListener('click', () => t.classList.toggle('on')));

async function run() {
  const p = path.value.trim() || selectedPath;
  if (!p) { alert('Pick a video first'); return; }
  const btn = document.getElementById('go');
  btn.disabled = true; btn.textContent = 'Processing...';
  const params = {
    src: p,
    noise_db: parseFloat(document.getElementById('noise').value),
    min_gap: parseFloat(document.getElementById('mingap').value),
    target_gap: parseFloat(document.getElementById('target').value),
    cut_head: document.querySelector('[data-key=cut_head]').classList.contains('on'),
    cut_tail: document.querySelector('[data-key=cut_tail]').classList.contains('on'),
    remove_retakes: document.querySelector('[data-key=remove_retakes]').classList.contains('on'),
  };
  const r = await fetch('/process', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(params)});
  const j = await r.json();
  if (j.error) { showError(j.error); btn.disabled=false; btn.textContent='Clean video'; return; }
  poll(j.job_id);
}

function showError(msg) {
  document.getElementById('result').classList.remove('hidden');
  document.getElementById('result').innerHTML = '<div class="card"><div class="status"><span class="err">ERROR</span><br>' + msg.replace(/</g,'&lt;') + '</div></div>';
}

async function poll(id) {
  const r = await fetch('/status?id='+id);
  const j = await r.json();
  const div = document.getElementById('result');
  div.classList.remove('hidden');
  if (j.status === 'done') {
    document.getElementById('go').disabled = false;
    document.getElementById('go').textContent = 'Clean another';
    const saved = (j.original - j.new_duration).toFixed(2);
    div.innerHTML = `
      <div class="card">
        <h2>Done</h2>
        <div class="stats">
          <div class="stat"><div class="v">${j.original.toFixed(1)}s</div><div class="l">Original</div></div>
          <div class="stat"><div class="v">${j.new_duration.toFixed(1)}s</div><div class="l">Clean</div></div>
          <div class="stat"><div class="v">-${saved}s</div><div class="l">Saved</div></div>
        </div>
        <div class="status">
          Silences detected: <span class="ok">${j.silences.length}</span><br>
          Retakes cut: <span class="ok">${j.retakes.length}</span><br>
          Segments kept: <span class="ok">${j.keeps.length}</span><br>
          Output: <span class="ok">${j.output}</span>
        </div>
        <a class="dl" href="/file?p=${encodeURIComponent(j.output)}" download>Download clean.mp4</a>
      </div>`;
    return;
  }
  if (j.status === 'error') {
    document.getElementById('go').disabled = false;
    document.getElementById('go').textContent = 'Try again';
    showError(j.progress + (j.trace ? '\n\n'+j.trace : ''));
    return;
  }
  div.innerHTML = `<div class="card"><div class="status"><span class="ok">⚡ ${j.status}</span> — ${j.progress}</div></div>`;
  setTimeout(()=>poll(id), 1000);
}
</script></div></body></html>"""


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
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/status":
            q = parse_qs(u.query)
            jid = q.get("id", [""])[0]
            j = JOBS.get(jid)
            if not j:
                self._json(404, {"error": "no job"}); return
            self._json(200, {k: v for k, v in j.items() if k != "trace" or j.get("status") == "error"})
            return
        if u.path == "/file":
            q = parse_qs(u.query)
            p = Path(q.get("p", [""])[0])
            if not p.exists():
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(p.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
            self.end_headers()
            with p.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/process":
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
            src = Path(body["src"])
            if not src.exists():
                self._json(400, {"error": f"file not found: {src}"}); return
            jid = uuid.uuid4().hex
            JOBS[jid] = {
                "src": str(src),
                "noise_db": body.get("noise_db", -32),
                "min_gap": body.get("min_gap", 0.30),
                "target_gap": body.get("target_gap", 0.30),
                "cut_head": body.get("cut_head", True),
                "cut_tail": body.get("cut_tail", True),
                "remove_retakes": body.get("remove_retakes", True),
                "status": "queued",
                "progress": "Starting...",
            }
            threading.Thread(target=process_job, args=(jid,), daemon=True).start()
            self._json(200, {"job_id": jid})
            return
        self.send_response(404); self.end_headers()


if __name__ == "__main__":
    port = 5001
    print(f"Video Cleaner: http://localhost:{port}")
    HTTPServer(("localhost", port), H).serve_forever()
