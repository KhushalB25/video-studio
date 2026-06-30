"""
Studio App — http://localhost:5000
Single-page video editor that runs locally. Pipeline:
  drop video -> auto-transcribe -> draft plan -> render preview
  tune positions/captions via sliders (live)
  type prompts -> queued for Claude Code to act on
  one-click render preview / render final

Claude Code reads <workdir>/prompt_queue.json when the user pings it in chat,
applies the asked edits to broll_plan.json, and the next render reflects.

Usage:
    python scripts/studio_app.py
"""
from __future__ import annotations
import json, os, re, shutil, subprocess, sys, time, hashlib, threading
import urllib.parse, mimetypes
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
VENV_PY = SKILL / ".venv/Scripts/python.exe" if sys.platform == "win32" else SKILL / ".venv/bin/python3"
PROPS = SKILL / "remotion/src/props.json"
CAP_TSX = SKILL / "remotion/src/templates/Captions.tsx"
# macOS AirPlay Receiver squats on :5000 (returns 403) — default off it. Override with STUDIO_PORT.
PORT = int(os.getenv("STUDIO_PORT", "5055"))

VERTICAL_KINDS = {"hook_title", "word_pop", "subscribe", "bar_overlay",
                  "tool_logo_burst", "portrait_burst", "bullet_burst",
                  "ratio_dots", "agent_avatar_burst", "inline_chart"}

STATE = {"video_path": None, "workdir": None, "status": "idle", "log": []}


def log(msg: str):
    STATE["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    STATE["log"] = STATE["log"][-100:]
    print(msg, flush=True)


def workdir_for(video_path: Path) -> Path:
    digest = hashlib.sha1(str(video_path.resolve()).encode()).hexdigest()[:12]
    return Path.home() / ".cache" / "video-edit" / f"{video_path.stem[:40]}_{digest}"


def plan_path() -> Path | None:
    if not STATE["workdir"]: return None
    return Path(STATE["workdir"]) / "broll_plan.json"


def source_path() -> Path | None:
    if not STATE["workdir"]: return None
    return Path(STATE["workdir"]) / "broll_plan.source.json"


def captions_path() -> Path | None:
    if not STATE["workdir"]: return None
    return Path(STATE["workdir"]) / "captions_plan.json"


def queue_path() -> Path:
    if STATE["workdir"]:
        return Path(STATE["workdir"]) / "prompt_queue.json"
    return SKILL / "prompt_queue.json"


# ─── plan + props mutators ───────────────────────────────────────────────

def all_plan_files() -> list[Path]:
    out = []
    for p in [PROPS, plan_path(), source_path()]:
        if p and p.exists(): out.append(p)
    return out


def load_props_beats():
    if not PROPS.exists(): return []
    d = json.loads(PROPS.read_text(encoding="utf-8"))
    out = []
    def walk(x):
        if isinstance(x, list):
            for i in x: walk(i)
        elif isinstance(x, dict):
            if x.get("kind") in VERTICAL_KINDS or x.get("kind") == "image_card":
                out.append(x)
            for v in x.values(): walk(v)
    walk(d)
    out.sort(key=lambda b: b.get("start_sec", 0))
    return out


def load_caps():
    if not PROPS.exists(): return []
    d = json.loads(PROPS.read_text(encoding="utf-8"))
    return d.get("captions", []) if isinstance(d, dict) else []


def write_beat_field(idx: int, field: str, value):
    beats = load_props_beats()
    if idx >= len(beats): return
    tgt_start = beats[idx].get("start_sec")
    tgt_kind = beats[idx].get("kind")
    for p in all_plan_files():
        d = json.loads(p.read_text(encoding="utf-8"))
        def walk(x):
            if isinstance(x, list):
                for i in x: walk(i)
            elif isinstance(x, dict):
                if x.get("kind") == tgt_kind and abs(x.get("start_sec", -999) - tgt_start) < 0.01:
                    if isinstance(value, float):
                        x[field] = round(value, 3)
                    else:
                        x[field] = value
                for v in x.values(): walk(v)
        walk(d)
        p.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="ascii")


def write_group_field(kind: str, field: str, value):
    for p in all_plan_files():
        d = json.loads(p.read_text(encoding="utf-8"))
        def walk(x):
            if isinstance(x, list):
                for i in x: walk(i)
            elif isinstance(x, dict):
                if x.get("kind") == kind:
                    if isinstance(value, float):
                        x[field] = round(value, 3)
                    else:
                        x[field] = value
                for v in x.values(): walk(v)
        walk(d)
        p.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="ascii")


def write_caption(idx: int | None, bottom_frac: float):
    cp = captions_path()
    if cp and cp.exists():
        d = json.loads(cp.read_text(encoding="utf-8"))
        if idx is None:
            for line in d: line["bottom_offset"] = round(bottom_frac, 3)
        elif idx < len(d):
            d[idx]["bottom_offset"] = round(bottom_frac, 3)
        cp.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="ascii")
    if PROPS.exists():
        d = json.loads(PROPS.read_text(encoding="utf-8"))
        caps = d.get("captions") if isinstance(d, dict) else None
        if isinstance(caps, list):
            if idx is None:
                for line in caps: line["bottom_offset"] = round(bottom_frac, 3)
            elif idx < len(caps):
                caps[idx]["bottom_offset"] = round(bottom_frac, 3)
            PROPS.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="ascii")
    if idx is None:
        text = CAP_TSX.read_text(encoding="utf-8")
        new = re.sub(r"(line\.bottom_offset \?\? \(isLandscape \? [\d.]+ : )[\d.]+(\))",
                     rf"\g<1>{round(bottom_frac,3)}\g<2>", text)
        CAP_TSX.write_text(new, encoding="utf-8")


# ─── pipeline actions ────────────────────────────────────────────────────

def run_pipeline(video_path: Path, mode: str = "preview"):
    """Run transcribe + render. mode in {'preview','final'}."""
    STATE["status"] = "transcribing"
    log(f"transcribing {video_path.name}")
    STATE["workdir"] = str(workdir_for(video_path))
    Path(STATE["workdir"]).mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([str(VENV_PY), str(SKILL/"scripts/transcribe.py"), str(video_path)],
                       check=True, cwd=str(SKILL))
        Path(STATE["workdir"], ".polished").touch()
    except subprocess.CalledProcessError as e:
        log(f"transcribe FAILED: {e}")
        STATE["status"] = "idle"
        return
    # If no plan, write a placeholder hint for Claude in queue
    if not plan_path().exists():
        log("no plan yet — drop a prompt or wait for Claude to author one")
    STATE["status"] = "rendering" if mode == "preview" else "rendering-final"
    log(f"render ({mode})")
    env = {"FORCE_RENDER": "1"}
    if mode == "final": env["QUALITY"] = "final"
    try:
        proc_env = {**__import__("os").environ, **env}
        subprocess.run(["bash", "scripts/render.sh", str(video_path)],
                       cwd=str(SKILL), env=proc_env, check=True)
        log(f"render done -> {video_path.stem}.{'enhanced' if mode=='final' else 'preview'}.mp4")
    except subprocess.CalledProcessError as e:
        log(f"render FAILED: {e}")
    STATE["status"] = "idle"


def render_async(video_path: Path, mode: str):
    threading.Thread(target=run_pipeline, args=(video_path, mode), daemon=True).start()


def enqueue_prompt(text: str):
    qp = queue_path()
    queue = json.loads(qp.read_text(encoding="utf-8")) if qp.exists() else []
    queue.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "prompt": text, "consumed": False})
    qp.write_text(json.dumps(queue, indent=2, ensure_ascii=True), encoding="ascii")
    log(f"prompt queued: {text[:60]}")


def get_queue_pending():
    qp = queue_path()
    if not qp.exists(): return []
    queue = json.loads(qp.read_text(encoding="utf-8"))
    return [q for q in queue if not q.get("consumed")]


# ─── HTML page ───────────────────────────────────────────────────────────

HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Video Studio</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,system-ui,sans-serif;background:#0F121A;color:#E9ECED;height:100vh;overflow:hidden;display:grid;grid-template-rows:48px 1fr 28px}
header{display:flex;align-items:center;gap:14px;padding:0 20px;background:#1E2434;border-bottom:1px solid #343E5B}
header h1{font-size:13px;color:#CFFF05;text-transform:uppercase;letter-spacing:.1em}
header .status{font-size:11px;color:#B5BFC2;font-family:monospace}
header .actions{margin-left:auto;display:flex;gap:8px}
button{background:#CFFF05;color:#0F121A;border:none;padding:7px 14px;border-radius:5px;font-weight:700;font-size:11px;cursor:pointer;text-transform:uppercase;letter-spacing:.04em;font-family:inherit}
button:hover{background:#B5E000}
button.ghost{background:#343E5B;color:#E9ECED}
button.ghost:hover{background:#454F6E}
button:disabled{opacity:.4;cursor:not-allowed}
.main{display:grid;grid-template-columns:300px 1fr 360px;overflow:hidden}
.col{overflow-y:auto;padding:14px;background:#0F121A;border-right:1px solid #1E2434}
.col:last-child{border-right:none;border-left:1px solid #1E2434}
.col h2{font-size:10px;text-transform:uppercase;color:#CFFF05;letter-spacing:.08em;margin-bottom:10px;margin-top:14px;padding-bottom:4px;border-bottom:1px solid #343E5B}
.col h2:first-child{margin-top:0}
.preview{background:#000;display:flex;align-items:center;justify-content:center;flex-direction:column;padding:20px;overflow:hidden}
video{max-width:100%;max-height:calc(100% - 40px);border-radius:8px;box-shadow:0 8px 32px rgba(0,0,0,.6)}
.dropzone{width:100%;max-width:480px;height:280px;border:2px dashed #343E5B;border-radius:12px;display:flex;align-items:center;justify-content:center;flex-direction:column;color:#B5BFC2;font-size:14px;cursor:pointer;transition:border-color .15s,background .15s}
.dropzone:hover,.dropzone.over{border-color:#CFFF05;background:rgba(207,255,5,.04)}
.dropzone .big{font-size:18px;color:#E9ECED;margin-bottom:8px}
.dropzone .hint{font-size:11px;color:#5B6478;margin-top:14px}
.video-name{margin-top:14px;color:#B5BFC2;font-size:11px;font-family:monospace}
textarea{width:100%;background:#1E2434;border:1px solid #343E5B;color:#E9ECED;border-radius:6px;padding:10px;font-family:inherit;font-size:12px;resize:vertical;min-height:90px}
textarea:focus{outline:none;border-color:#CFFF05}
.row{display:grid;grid-template-columns:1fr 56px;gap:8px;align-items:center;padding:5px 0;font-size:11px}
.row .lbl{color:#D2D8DA;display:flex;justify-content:space-between;align-items:baseline;gap:6px}
.row .lbl .pill{display:inline-block;background:#343E5B;color:#CFFF05;font-size:9px;font-family:monospace;padding:1px 4px;border-radius:3px;margin-right:4px}
.row .lbl .sub{color:#5B6478;font-size:10px;display:block;margin-top:1px}
input[type=range]{width:100%;accent-color:#CFFF05}
input[type=number],select{background:#0F121A;border:1px solid #343E5B;color:#E9ECED;padding:3px 4px;border-radius:3px;font-size:10px;font-family:monospace;width:100%}
.beat-grp{background:#1E2434;border-radius:6px;padding:10px;margin-bottom:8px}
.beat-grp .head{display:flex;justify-content:space-between;align-items:center;cursor:pointer;font-size:11px;font-weight:600;color:#CFFF05;text-transform:uppercase;letter-spacing:.04em}
.beat-grp .body{margin-top:8px;display:none}
.beat-grp.open .body{display:block}
.beat-grp .head::after{content:"▸";color:#5B6478;font-size:9px}
.beat-grp.open .head::after{content:"▾"}
.group-row{background:#343E5B;padding:7px 10px;border-radius:5px;margin-bottom:6px;display:grid;grid-template-columns:1fr 56px;gap:8px;align-items:center;font-size:11px;color:#CFFF05;text-transform:uppercase;font-weight:600;letter-spacing:.03em}
.queue-item{background:#1E2434;border-left:3px solid #CFFF05;padding:8px 10px;margin-bottom:6px;font-size:11px;border-radius:0 4px 4px 0}
.queue-item .ts{color:#5B6478;font-size:9px;font-family:monospace}
.queue-item .txt{color:#D2D8DA;margin-top:3px}
.log{padding:0 20px;display:flex;align-items:center;background:#0a0c12;border-top:1px solid #1E2434;color:#5B6478;font-family:monospace;font-size:10px;overflow:hidden}
.log-msg{white-space:nowrap;text-overflow:ellipsis;overflow:hidden}
.empty{color:#5B6478;font-size:11px;font-style:italic;padding:8px 0}
</style></head><body>

<header>
  <h1>Video Studio</h1>
  <span class="status" id="status">idle</span>
  <div class="actions">
    <button class="ghost" onclick="openStudio()">Open Remotion Studio</button>
    <button onclick="renderMode('preview')" id="btn-preview">Render Preview</button>
    <button onclick="renderMode('final')" id="btn-final">Render Final (1080p)</button>
  </div>
</header>

<div class="main">
  <div class="col" id="col-left">
    <h2>Source video</h2>
    <div id="video-info"></div>

    <h2>Prompt Claude</h2>
    <textarea id="prompt" placeholder="e.g. 'move the hook lower' / 'add a stat for 30 bucks' / 'shorter captions' — gets queued; ping Claude in chat to apply"></textarea>
    <button onclick="sendPrompt()" style="margin-top:8px;width:100%">Queue Prompt</button>

    <h2>Pending prompts</h2>
    <div id="queue"></div>
  </div>

  <div class="preview">
    <div id="dropzone" class="dropzone">
      <div class="big">Drop video here</div>
      <div>or click to choose</div>
      <div class="hint">MP4, MOV, WebM. Auto-transcribes + renders preview.</div>
      <input type="file" id="file" style="display:none" accept="video/*"/>
    </div>
    <video id="vid" controls style="display:none"></video>
    <div class="video-name" id="vidname"></div>
  </div>

  <div class="col" id="col-right">
    <h2>Group set (all of kind)</h2>
    <div id="groups"></div>

    <h2>Individual beats</h2>
    <div id="beats"></div>

    <h2>Captions</h2>
    <div id="caps-default"></div>
    <div id="caps-lines"></div>
  </div>
</div>

<div class="log"><span class="log-msg" id="log">ready</span></div>

<script>
const $ = id => document.getElementById(id);
let STATE = {};

async function refresh(){
  const r = await fetch('/state'); STATE = await r.json();
  $('status').textContent = STATE.status;
  $('log').textContent = (STATE.log||[]).slice(-1)[0] || 'ready';
  $('video-info').innerHTML = STATE.video_path
    ? `<div style="background:#1E2434;padding:8px;border-radius:5px;font-size:11px"><div style="color:#CFFF05;text-transform:uppercase;font-size:9px;margin-bottom:3px">loaded</div>${STATE.video_path.split(/[\\\\/]/).pop()}</div>`
    : '<div class="empty">No video loaded</div>';
  if(STATE.preview_url){
    $('vid').src = STATE.preview_url + '?t=' + Date.now();
    $('vid').style.display = 'block';
    $('dropzone').style.display = 'none';
    $('vidname').textContent = STATE.preview_basename || '';
  }
  renderTuner();
  renderQueue();
  $('btn-preview').disabled = !STATE.video_path || STATE.status.includes('render');
  $('btn-final').disabled = !STATE.video_path || STATE.status.includes('render');
}

function renderTuner(){
  // groups
  let g = '';
  const groupKinds = {};
  (STATE.beats||[]).forEach(b => { if(b.vertical !== undefined) groupKinds[b.kind] = b.vertical; });
  Object.entries(groupKinds).sort().forEach(([k,v]) => {
    g += `<div class="group-row">
      <div>${k}</div><div></div>
      <input type="range" min="0" max="1" step="0.01" value="${v}" oninput="setGroup('${k}','vertical',this.value); n_g_${k}.value=this.value"/>
      <input type="number" id="n_g_${k}" min="0" max="1" step="0.01" value="${v}" oninput="setGroup('${k}','vertical',this.value)"/>
    </div>`;
  });
  $('groups').innerHTML = g || '<div class="empty">No beats yet</div>';
  // individual
  let b = '';
  const byKind = {};
  (STATE.beats||[]).forEach((bt,i) => { (byKind[bt.kind] = byKind[bt.kind]||[]).push([i,bt]); });
  Object.keys(byKind).sort().forEach(k => {
    b += `<div class="beat-grp" onclick="this.classList.toggle('open')"><div class="head"><span>${k} (${byKind[k].length})</span></div><div class="body" onclick="event.stopPropagation()">`;
    byKind[k].forEach(([i,bt]) => {
      const snip = (bt.title||bt.caption||bt.value||(bt.items&&bt.items[0]&&bt.items[0].text)||bt.kicker||'').substring(0,28);
      b += `<div style="border-top:1px solid #343E5B;padding:8px 0;margin-top:6px"><div style="font-size:10px;color:#5B6478;margin-bottom:4px"><span class="pill">${bt.start_sec.toFixed(1)}s</span> ${snip}</div>`;
      if(bt.vertical !== undefined){
        b += sliderRow('vertical', i, 'vertical', bt.vertical, 0, 1, 0.01);
      }
      if(bt.kind === 'image_card'){
        b += sliderRow('card_top', i, 'card_top', bt.card_top||0.6, 0, 1, 0.01);
        b += sliderRow('card_bottom', i, 'card_bottom', bt.card_bottom||0.04, 0, 0.5, 0.01);
        b += sliderRow('card_margin', i, 'card_margin', bt.card_margin||0.06, 0, 0.3, 0.01);
        b += sliderRow('image_scale', i, 'image_scale', bt.image_scale||1.0, 0.3, 2, 0.05);
        b += `<div class="row"><div class="lbl">card_fit</div><select onchange="setBeatStr(${i},'card_fit',this.value)"><option value="wide" ${bt.card_fit==='wide'||!bt.card_fit?'selected':''}>wide</option><option value="hug" ${bt.card_fit==='hug'?'selected':''}>hug</option></select></div>`;
      }
      b += '</div>';
    });
    b += '</div></div>';
  });
  $('beats').innerHTML = b || '<div class="empty">No beats yet</div>';
  // caption default
  const capD = STATE.caption_default || 0.16;
  $('caps-default').innerHTML = `<div class="group-row"><div>all captions</div><div></div>
    <input type="range" min="0" max="0.5" step="0.005" value="${capD}" oninput="setCapDefault(this.value); n_capd.value=this.value"/>
    <input type="number" id="n_capd" min="0" max="0.5" step="0.005" value="${capD}" oninput="setCapDefault(this.value)"/>
  </div>`;
  // caption lines
  let cl = '<div class="beat-grp" onclick="this.classList.toggle(\\'open\\')"><div class="head"><span>per-line (${(STATE.caps||[]).length})</span></div><div class="body" onclick="event.stopPropagation()">';
  (STATE.caps||[]).forEach((c,i) => {
    const txt = (c.words||[]).map(w=>w.text).join(' ').substring(0,30);
    const v = c.bottom_offset || capD;
    cl += `<div style="border-top:1px solid #343E5B;padding:6px 0;margin-top:4px"><div style="font-size:10px;color:#5B6478;margin-bottom:3px"><span class="pill">${c.start_sec.toFixed(1)}s</span> ${txt}</div>
      ${sliderRow('bottom_offset', i, 'bottom_offset_cap', v, 0, 0.5, 0.005)}</div>`;
  });
  cl += '</div></div>';
  $('caps-lines').innerHTML = cl;
}

function sliderRow(label, idx, field, val, mn, mx, step){
  const isCap = field === 'bottom_offset_cap';
  const handler = isCap ? `setCap(${idx},this.value)` : `setBeat(${idx},'${field}',this.value)`;
  const id = `nf_${idx}_${field}`;
  return `<div class="row"><div class="lbl">${label}</div><div></div>
    <input type="range" min="${mn}" max="${mx}" step="${step}" value="${val}" oninput="${handler}; ${id}.value=this.value"/>
    <input type="number" id="${id}" min="${mn}" max="${mx}" step="${step}" value="${val}" oninput="${handler}"/>
  </div>`;
}

function renderQueue(){
  const pending = (STATE.queue||[]).filter(q => !q.consumed);
  $('queue').innerHTML = pending.length
    ? pending.map(q => `<div class="queue-item"><div class="ts">${q.ts}</div><div class="txt">${escapeHtml(q.prompt)}</div></div>`).join('')
    : '<div class="empty">No prompts queued. Type one above + ping Claude in chat.</div>';
}

function escapeHtml(s){return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function setBeat(i,f,v){fetch(`/api/beat?idx=${i}&field=${f}&value=${v}`);}
function setBeatStr(i,f,v){fetch(`/api/beat?idx=${i}&field=${f}&value=${v}&str=1`);}
function setGroup(k,f,v){fetch(`/api/group?kind=${k}&field=${f}&value=${v}`);}
function setCap(i,v){fetch(`/api/cap?idx=${i}&value=${v}`);}
function setCapDefault(v){fetch(`/api/cap?value=${v}`);}

async function sendPrompt(){
  const t = $('prompt').value.trim(); if(!t) return;
  await fetch('/api/prompt', {method:'POST', body:t});
  $('prompt').value = ''; refresh();
}

function renderMode(m){
  fetch(`/api/render?mode=${m}`, {method:'POST'});
  setTimeout(refresh, 500);
}

function openStudio(){window.open('http://localhost:3001', '_blank');}

// drag-drop
const dz = $('dropzone'); const fi = $('file');
dz.onclick = () => fi.click();
fi.onchange = e => upload(e.target.files[0]);
['dragover','dragenter'].forEach(ev => dz.addEventListener(ev, e => {e.preventDefault(); dz.classList.add('over');}));
['dragleave','drop'].forEach(ev => dz.addEventListener(ev, e => {e.preventDefault(); dz.classList.remove('over');}));
dz.addEventListener('drop', e => upload(e.dataTransfer.files[0]));

async function upload(file){
  if(!file) return;
  const fd = new FormData(); fd.append('video', file);
  $('log').textContent = 'uploading ' + file.name + '...';
  const r = await fetch('/api/upload', {method:'POST', body:fd});
  if(r.ok) refresh();
}

refresh(); setInterval(refresh, 2000);
</script>
</body></html>
"""


# ─── HTTP handlers ───────────────────────────────────────────────────────

class H(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body, code=200):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _ok(self):
        self.send_response(200); self.end_headers()

    def _state(self):
        beats = load_props_beats()
        caps = load_caps()
        # caption default from Captions.tsx
        cap_default = 0.16
        if CAP_TSX.exists():
            m = re.search(r"line\.bottom_offset \?\? \(isLandscape \? [\d.]+ : ([\d.]+)\)",
                          CAP_TSX.read_text(encoding="utf-8"))
            if m: cap_default = float(m.group(1))
        # preview file
        preview_url = None
        preview_basename = None
        if STATE["video_path"]:
            vp = Path(STATE["video_path"])
            for suffix in [".enhanced.mp4", ".preview.mp4"]:
                cand = vp.parent / (vp.stem + suffix)
                if cand.exists():
                    preview_url = f"/file?p={urllib.parse.quote(str(cand))}"
                    preview_basename = cand.name
                    break
        # queue
        qp = queue_path()
        queue = json.loads(qp.read_text(encoding="utf-8")) if qp.exists() else []
        return {
            "video_path": STATE["video_path"],
            "workdir": STATE["workdir"],
            "status": STATE["status"],
            "log": STATE["log"][-30:],
            "beats": beats,
            "caps": caps,
            "caption_default": cap_default,
            "preview_url": preview_url,
            "preview_basename": preview_basename,
            "queue": queue,
        }

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            self._html(HTML); return
        if u.path == "/state":
            self._json(self._state()); return
        if u.path == "/file":
            p = Path(q.get("p", [""])[0])
            if not p.exists():
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(p.stat().st_size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with p.open("rb") as f: shutil.copyfileobj(f, self.wfile)
            return
        if u.path == "/api/beat":
            try:
                idx = int(q["idx"][0]); field = q["field"][0]; raw = q["value"][0]
                val = raw if q.get("str") else float(raw)
                write_beat_field(idx, field, val)
            except Exception as e: log(f"beat err: {e}")
            self._ok(); return
        if u.path == "/api/group":
            try:
                k = q["kind"][0]; f = q["field"][0]; v = float(q["value"][0])
                write_group_field(k, f, v)
            except Exception as e: log(f"group err: {e}")
            self._ok(); return
        if u.path == "/api/cap":
            try:
                v = float(q["value"][0])
                idx = int(q["idx"][0]) if "idx" in q else None
                write_caption(idx, v)
            except Exception as e: log(f"cap err: {e}")
            self._ok(); return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/upload":
            # parse multipart
            ctype = self.headers.get("Content-Type", "")
            m = re.match(r"multipart/form-data;\s*boundary=(.+)", ctype)
            if not m:
                self.send_response(400); self.end_headers(); return
            boundary = b"--" + m.group(1).encode()
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length)
            parts = data.split(boundary)
            for part in parts:
                if b'filename="' not in part: continue
                fn_m = re.search(rb'filename="([^"]+)"', part)
                if not fn_m: continue
                filename = fn_m.group(1).decode("utf-8")
                body_start = part.find(b"\r\n\r\n") + 4
                body_end = part.rfind(b"\r\n")
                body = part[body_start:body_end]
                up_dir = Path.home() / "Downloads"
                target = up_dir / filename
                target.write_bytes(body)
                STATE["video_path"] = str(target)
                log(f"uploaded {filename} ({len(body)//1024} KB)")
                render_async(target, "preview")
                break
            self._ok(); return
        if u.path == "/api/render":
            mode = q.get("mode", ["preview"])[0]
            if STATE["video_path"]:
                render_async(Path(STATE["video_path"]), mode)
            self._ok(); return
        if u.path == "/api/prompt":
            length = int(self.headers.get("Content-Length", 0))
            text = self.rfile.read(length).decode("utf-8").strip()
            if text:
                enqueue_prompt(text)
            self._ok(); return
        self.send_response(404); self.end_headers()


# ─── main ────────────────────────────────────────────────────────────────

def main():
    # bootstrap from existing preview if present
    dwn = Path.home() / "Downloads"
    cands = list(dwn.glob("WhatsApp Video 2026-06-27.mp4")) + list(dwn.glob("*.mp4"))
    for c in cands:
        if c.exists() and not c.name.endswith(".preview.mp4") and not c.name.endswith(".enhanced.mp4"):
            STATE["video_path"] = str(c)
            STATE["workdir"] = str(workdir_for(c))
            break
    print(f"Video Studio: http://localhost:{PORT}")
    print(f"(Remotion Studio on http://localhost:3001 — open separately if you want template HMR)")
    HTTPServer(("localhost", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
