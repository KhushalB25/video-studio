"""Phase 2 Editor — http://localhost:5002
Upload clean video, analyze, chat-confirm plan, edit, preview in Studio, tune, render.
"""
from __future__ import annotations
import json, hashlib, subprocess, threading, uuid, shutil, html, re, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SKILL = Path(__file__).resolve().parent.parent
VENV_PY = SKILL / ".venv/Scripts/python.exe"
TRANSCRIBE = SKILL / "scripts/transcribe.py"
RENDER_SH = SKILL / "scripts/render.sh"
WORK_ROOT = Path.home() / ".cache/video-edit"
SESSIONS: dict[str, dict] = {}


def _run(cmd, **kw):
    return subprocess.run(cmd, **kw)


def workdir_for(src: Path) -> Path:
    digest = hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12]
    d = WORK_ROOT / f"{src.stem[:40]}_{digest}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def append_chat(sid: str, role: str, text: str, typ: str = "msg", meta: dict | None = None):
    sess = SESSIONS[sid]
    chat = sess.setdefault("chat", [])
    chat.append({"role": role, "text": text, "type": typ, "ts": time.time(), "meta": meta or {}})
    # persist
    wd = Path(sess["workdir"])
    (wd / "chat.json").write_text(json.dumps(chat, indent=2, ensure_ascii=True), encoding="ascii")


def write_queue(sid: str, action: str, prompt: str, context: dict):
    sess = SESSIONS[sid]
    wd = Path(sess["workdir"])
    qp = wd / "prompt_queue.json"
    q = json.loads(qp.read_text(encoding="utf-8")) if qp.exists() else []
    q.append({
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "action": action,
        "user_prompt": prompt,
        "context": context,
        "consumed": False,
        "result": None,
    })
    qp.write_text(json.dumps(q, indent=2, ensure_ascii=True), encoding="ascii")


def transcribe(src: Path, wd: Path) -> list[dict]:
    out = wd / "words.json"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return json.loads(out.read_text(encoding="utf-8"))
    _run([str(VENV_PY), str(TRANSCRIBE), str(src)], check=True)
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    return []


def auto_analyze(sid: str):
    sess = SESSIONS[sid]
    src = Path(sess["src"])
    wd = Path(sess["workdir"])

    append_chat(sid, "assistant", "Transcribing audio...", "status")
    words = transcribe(src, wd)
    full_text = " ".join(w["word"] for w in words)
    duration = words[-1]["end"] if words else 0

    # post transcript summary
    append_chat(sid, "assistant",
        f"**Transcript ({duration:.1f}s, {len(words)} words):**\n\n_{full_text}_",
        "transcript", {"words_count": len(words), "duration": duration})

    # generate a baseline suggestion (heuristic-driven, then upgradable via Claude)
    suggestions = []
    suggestions.append(f"Duration: **{duration:.1f}s** ({'short-form (9:16)' if duration < 60 else 'long-form'} mode).")
    # detect numbers, brands, list markers
    nums = re.findall(r"\b\d+%|\$\d+|\d+\s?(?:bucks|dollars|million|billion|months?|years?|days?|hours?)\b", full_text, re.I)
    if nums:
        suggestions.append(f"Detected hero numbers/stats: `{', '.join(nums[:5])}` → use `stat_punch` beats for the top 1-2.")
    brand_hits = re.findall(r"\b(?:claude|chatgpt|openai|anthropic|chrome|google|stripe|figma|notion|kickbacks|cursor|github|vercel|youtube|instagram|tiktok)\b", full_text, re.I)
    brand_hits = list(dict.fromkeys(b.lower() for b in brand_hits))
    if brand_hits:
        suggestions.append(f"Named brands: `{', '.join(brand_hits)}` → `tool_logo_burst` w/ fetched logos.")
    if re.search(r"\b(?:first|second|third|three reasons|few steps|steps)\b", full_text, re.I):
        suggestions.append("Speaker enumerates → use `vertical_timeline` or `bullet_burst`.")
    if re.search(r"(?:vs|versus|but|instead|however|compared)", full_text, re.I):
        suggestions.append("Contrast/pivot moment detected → use `word_pop` framing the shift (NOT vs_split — banned).")
    suggestions.append("Plan to include: `hook_title` at 0s, captions auto-burned, `subscribe` button at CTA close.")

    plan_msg = "**My suggested edit plan:**\n\n" + "\n".join(f"- {s}" for s in suggestions)
    plan_msg += "\n\n**To customize further** — type feedback in the chat below (e.g. 'add a bar chart at the 50% mention', 'no captions', 'use upbeat music')."
    plan_msg += "\n\n_Once happy, click **Start Editing** below._"
    append_chat(sid, "assistant", plan_msg, "plan")
    sess["status"] = "awaiting_approval"


BRAND_PATTERN = re.compile(
    r"\b(claude|chatgpt|gpt-?4|openai|anthropic|chrome|google|stripe|figma|notion|"
    r"kickbacks|cursor|github|vercel|youtube|instagram|tiktok|x\.com|twitter|"
    r"linkedin|reddit|discord|slack|zoom|spotify|netflix|apple|microsoft|"
    r"meta|tesla|nvidia|amazon|shopify)\b", re.I)
NUMBER_PATTERN = re.compile(r"\b(\$\d+(?:[.,]\d+)*[kKmMbB]?|\d+%|\d+x|"
                            r"\d+\s?(?:bucks|dollars|million|billion|hundred|thousand|months?|years?|days?|hours?|minutes?))\b", re.I)


def _find_word_idx(words, target_text, start_search=0):
    """Find first word index whose text matches (case-insensitive, punct-stripped)."""
    t = target_text.lower().strip(".,!?\"'")
    for i in range(start_search, len(words)):
        if words[i]["word"].lower().strip(".,!?\"'") == t:
            return i
    return -1


def generate_rule_plan(words: list[dict], user_notes: list[str]) -> list[dict]:
    """Rule-based broll plan from transcript heuristics. Skill-rule compliant."""
    if not words:
        return []
    duration = words[-1]["end"]
    full = " ".join(w["word"] for w in words)
    plan = []

    # 1. HOOK_TITLE at 0-2.5s (always)
    first_5 = " ".join(w["word"] for w in words[:6]).upper().strip(".,!?\"'")
    short_title = first_5[:18] if len(first_5) > 6 else "WATCH THIS"
    plan.append({
        "kind": "hook_title",
        "start_sec": 0.0,
        "end_sec": min(2.6, duration * 0.08),
        "kicker": "INTRO",
        "title": short_title,
        "vertical": 0.78,
        "reason": "Cold-open hook"
    })

    # Track used time spans to avoid overlap (rule 4o density cap)
    used_spans = [(0.0, plan[0]["end_sec"])]

    def fits(start, end):
        if end > duration - 1.0: return False
        for us, ue in used_spans:
            if not (end <= us or start >= ue):
                return False
        return True

    # 2. NUMBERS -> stat_punch (one or two heroes)
    stat_count = 0
    for m in NUMBER_PATTERN.finditer(full):
        if stat_count >= 2: break
        value = m.group(1).upper().replace(" ", "")
        # find word position for this number
        toks = value.lower().split()
        first_tok = toks[0].strip("$%")
        for i, w in enumerate(words):
            wt = w["word"].lower().strip(".,!?\"'$%")
            if wt == first_tok or wt.startswith(first_tok):
                start = w["start"] - 0.2
                end = min(w["start"] + 3.0, duration - 1.0)
                if fits(start, end):
                    plan.append({
                        "kind": "stat_punch",
                        "start_sec": round(start, 2),
                        "end_sec": round(end, 2),
                        "value": value,
                        "caption": "HERO NUMBER",
                        "speech_anchor": w["word"],
                        "reason": "Spoken hero number"
                    })
                    used_spans.append((start, end))
                    stat_count += 1
                break

    # 3. BRANDS -> tool_logo_burst (single multi-brand if multiple, else skip)
    brands = []
    seen = set()
    for i, w in enumerate(words):
        wt = w["word"].lower().strip(".,!?\"'")
        if BRAND_PATTERN.fullmatch(wt) and wt not in seen:
            seen.add(wt)
            brands.append((i, wt))
        if len(brands) >= 4: break
    if len(brands) >= 2:
        # span: first brand to last brand + 2s
        start = max(words[brands[0][0]]["start"] - 0.3, 0)
        end = min(words[brands[-1][0]]["end"] + 1.5, duration - 1.0)
        if fits(start, end):
            items = []
            for idx, bname in brands:
                logo_file = f"{bname}.png" if bname != "chrome" else "google_chrome.png"
                items.append({
                    "image_path": logo_file,
                    "label": bname.upper(),
                    "appear_sec": round(words[idx]["start"], 2),
                })
            if items:
                items[-1]["accent"] = True
            plan.append({
                "kind": "tool_logo_burst",
                "start_sec": round(start, 2),
                "end_sec": round(end, 2),
                "vertical": 0.45,
                "items": items,
                "speech_anchor": brands[0][1],
                "reason": "Speaker names brands"
            })
            used_spans.append((start, end))

    # 4. CONTRAST phrases -> word_pop (one mid-video)
    contrast_re = re.compile(r"\b(instead|but|however|actually|honestly)\b", re.I)
    placed_wp = 0
    for w in words:
        if placed_wp >= 2: break
        if contrast_re.fullmatch(w["word"].strip(".,!?\"'")):
            start = w["start"] - 0.1
            end = min(start + 2.5, duration - 1.0)
            if fits(start, end):
                # pick next ~3 words as text
                idx = words.index(w)
                phrase = " ".join(x["word"] for x in words[idx:idx+4]).strip(".,")
                plan.append({
                    "kind": "word_pop",
                    "start_sec": round(start, 2),
                    "end_sec": round(end, 2),
                    "vertical": 0.78,
                    "items": [{"text": "{" + phrase[:28] + "}", "appear_sec": round(start + 0.1, 2), "accent": True}],
                    "speech_anchor": w["word"],
                    "reason": "Contrast/pivot moment"
                })
                used_spans.append((start, end))
                placed_wp += 1

    # 5. SUBSCRIBE at the very end (always)
    sub_start = max(duration - 2.5, duration * 0.92)
    sub_end = duration - 0.05
    plan.append({
        "kind": "subscribe",
        "start_sec": round(sub_start, 2),
        "end_sec": round(sub_end, 2),
        "vertical": 0.88,
        "reason": "CTA close"
    })

    # Sort by start_sec
    plan.sort(key=lambda b: b["start_sec"])
    return plan


def run_edit(sid: str):
    sess = SESSIONS[sid]
    sess["status"] = "editing"
    src = Path(sess["src"])
    wd = Path(sess["workdir"])
    append_chat(sid, "assistant", "**Generating beat plan from transcript heuristics (no AI call).** Rendering preview...", "status")

    # Hybrid step 1: rule-based plan, instant
    words = json.loads((wd / "words.json").read_text(encoding="utf-8"))
    plan = generate_rule_plan(words, sess.get("user_notes", []))
    plan_path = wd / "broll_plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=True), encoding="ascii")
    append_chat(sid, "assistant",
        f"**Plan generated** — {len(plan)} beats: " + ", ".join(b["kind"] for b in plan) +
        "\n\nNow rendering preview...", "plan")

    # Also need to copy plan to render.sh's workdir (hash mismatch workaround)
    _copy_to_render_workdir(src, wd)

    _do_render(sid)


def _copy_to_render_workdir(src: Path, edit_wd: Path):
    """render.sh computes a different workdir hash on Windows (bash path vs Python path).
    Copy plan + words + broll/ to render.sh's workdir so the render picks them up."""
    import hashlib
    # mimic bash's `$(cd $(dirname) && pwd)/$(basename)` style path
    bash_path = "/" + str(src).replace("\\", "/").replace(":", "").lower()
    if bash_path[1].isalpha():
        bash_path = "/" + bash_path[1] + bash_path[2:]
    # Actually try both common hashes — Python and bash forms
    digests = [
        hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12],
        hashlib.sha1(bash_path.encode()).hexdigest()[:12],
        hashlib.sha1(str(src).replace("\\", "/").encode()).hexdigest()[:12],
    ]
    root = Path.home() / ".cache" / "video-edit"
    for d in digests:
        target = root / f"{src.stem[:40]}_{d}"
        if target == edit_wd: continue
        if target.exists():
            for f in ("broll_plan.json", "words.json"):
                if (edit_wd / f).exists():
                    shutil.copy(edit_wd / f, target / f)
            if (edit_wd / "broll").exists():
                target_broll = target / "broll"
                target_broll.mkdir(exist_ok=True)
                for f in (edit_wd / "broll").iterdir():
                    shutil.copy(f, target_broll)
            (target / ".polished").touch()
            (target / "broll_plan.source.json").unlink(missing_ok=True)
        else:
            target.mkdir(parents=True, exist_ok=True)
            for f in ("broll_plan.json", "words.json"):
                if (edit_wd / f).exists():
                    shutil.copy(edit_wd / f, target / f)
            if (edit_wd / "broll").exists():
                target_broll = target / "broll"
                target_broll.mkdir(exist_ok=True)
                for f in (edit_wd / "broll").iterdir():
                    shutil.copy(f, target_broll)
            (target / ".polished").touch()


def _wait_for_plan_and_render(sid: str):
    sess = SESSIONS[sid]
    wd = Path(sess["workdir"])
    plan_path = wd / "broll_plan.json"
    for _ in range(600):  # 10 min max
        if plan_path.exists():
            append_chat(sid, "assistant", "**Plan received from Claude.** Rendering preview now...", "status")
            _do_render(sid)
            return
        time.sleep(1)
    append_chat(sid, "assistant", "**Timeout waiting for Claude.** Author broll_plan.json manually in the workdir, then click Start Editing again.", "error")


def _do_render(sid: str):
    sess = SESSIONS[sid]
    src = Path(sess["src"])
    wd = Path(sess["workdir"])
    try:
        env = {"FORCE_RENDER": "1", **dict(__import__("os").environ)}
        _run(["bash", str(RENDER_SH), str(src)], check=True, env=env, cwd=str(SKILL))
        preview = src.parent / f"{src.stem}.preview.mp4"
        if preview.exists():
            sess["preview_path"] = str(preview)
            sess["status"] = "preview_ready"
            append_chat(sid, "assistant",
                f"**✅ Editing complete!**\n\nOpen **Remotion Studio** to preview + tune placements:\nclick the **Open Remotion Studio** button below.\n\nUse the **Tuner panel** on the right to adjust any element. When you're happy, click **Render Final** to export at full quality.",
                "done", {"preview": str(preview)})
        else:
            sess["status"] = "error"
            append_chat(sid, "assistant", "Render finished but no preview file found.", "error")
    except subprocess.CalledProcessError as e:
        sess["status"] = "error"
        append_chat(sid, "assistant", f"Render failed: {e}", "error")


def launch_studio():
    """Launch Remotion Studio on 3001 if not already running."""
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 3001))
        s.close()
        # not running → launch
        subprocess.Popen(["npx", "--no-install", "remotion", "studio", "src/index.ts", "--port", "3001"],
                         cwd=str(SKILL / "remotion"), creationflags=0x08000000 if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
        time.sleep(2)
        return True
    except OSError:
        s.close()
        return False  # already running


def final_render(sid: str):
    sess = SESSIONS[sid]
    sess["status"] = "rendering_final"
    src = Path(sess["src"])
    append_chat(sid, "assistant", "**Rendering final 1080p w/ music + SFX.** ~60s.", "status")
    try:
        env = {"QUALITY": "final", "FORCE_RENDER": "1", **dict(__import__("os").environ)}
        _run(["bash", str(RENDER_SH), str(src)], check=True, env=env, cwd=str(SKILL))
        final = src.parent / f"{src.stem}.enhanced.mp4"
        if final.exists():
            sess["final_path"] = str(final)
            sess["status"] = "done"
            append_chat(sid, "assistant",
                f"**🎬 Final video ready!**\n\nFile: `{final}`\n\nClick **Download** below.",
                "final_done", {"final": str(final)})
    except subprocess.CalledProcessError as e:
        sess["status"] = "error"
        append_chat(sid, "assistant", f"Final render failed: {e}", "error")


HTML_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Video Editor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,'Segoe UI',sans-serif}
body{background:#0F121A;color:#E9ECED;min-height:100vh;display:grid;grid-template-rows:auto 1fr auto;height:100vh}
header{padding:16px 24px;background:#1E2434;border-bottom:1px solid #2a3247;display:flex;justify-content:space-between;align-items:center}
header h1{color:#CFFF05;font-size:14px;text-transform:uppercase;letter-spacing:.08em;font-weight:900}
header .actions{display:flex;gap:10px}
.actbtn{background:#0F121A;border:1px solid #343E5B;color:#B5BFC2;padding:8px 14px;border-radius:5px;font-size:11px;cursor:pointer;text-transform:uppercase;letter-spacing:.05em;font-weight:600;text-decoration:none;display:inline-block}
.actbtn:hover{border-color:#CFFF05;color:#CFFF05}
.actbtn.primary{background:#CFFF05;color:#0F121A;border-color:#CFFF05;font-weight:800}
.actbtn.primary:hover{background:#dfff45}
.actbtn:disabled{opacity:.4;cursor:not-allowed}
main{display:grid;grid-template-columns:1fr 380px;overflow:hidden}
.chat{padding:24px;overflow-y:auto;background:#0F121A}
.msg{padding:14px 18px;border-radius:10px;margin-bottom:14px;max-width:680px;line-height:1.55;font-size:13px}
.msg.assistant{background:#1E2434;border-left:3px solid #CFFF05}
.msg.user{background:#252b3d;border-left:3px solid #B5BFC2;margin-left:60px}
.msg.transcript{background:#1a1f30;border-left:3px solid #5B6478;font-style:italic;color:#B5BFC2}
.msg.status{background:transparent;color:#7a8497;font-size:11px;text-transform:uppercase;letter-spacing:.06em;padding:6px 0;border:none}
.msg.error{background:#3a1a1a;border-left:3px solid #ff6b6b;color:#ffb3b3}
.msg.done{background:#1a2f1a;border-left:3px solid #CFFF05}
.msg .role{font-size:9px;color:#7a8497;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}
.msg strong{color:#CFFF05;font-weight:700}
.msg code{background:#0F121A;padding:1px 5px;border-radius:3px;font-family:'Consolas',monospace;font-size:11px;color:#dfff45}
.side{background:#1E2434;border-left:1px solid #2a3247;padding:20px;overflow-y:auto;display:flex;flex-direction:column;gap:14px}
.side h2{font-size:10px;color:#CFFF05;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px}
.card{background:#0F121A;border:1px solid #2a3247;border-radius:8px;padding:14px}
.upload{border:2px dashed #343E5B;border-radius:8px;padding:24px;text-align:center;cursor:pointer}
.upload:hover{border-color:#CFFF05}
.upload .b{font-size:12px;color:#E9ECED;font-weight:600;margin-bottom:6px}
.upload .s{font-size:10px;color:#7a8497}
.upload input[type=text]{width:100%;background:#0F121A;border:1px solid #343E5B;color:#E9ECED;padding:6px 8px;border-radius:4px;font-size:10px;font-family:monospace;margin-top:10px}
.preview{width:100%;border-radius:6px;background:#0F121A}
.stage{font-size:10px;color:#7a8497;text-transform:uppercase;letter-spacing:.06em;text-align:center;padding:4px 8px;background:#0F121A;border-radius:4px}
.stage.active{color:#CFFF05;background:#1a2030;border:1px solid #CFFF05}
.steps{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:8px}
footer{background:#1E2434;border-top:1px solid #2a3247;padding:14px 24px;display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center}
.input{background:#0F121A;border:1px solid #343E5B;color:#E9ECED;padding:10px 14px;border-radius:6px;font-size:13px;width:100%}
.input:focus{outline:1px solid #CFFF05;border-color:#CFFF05}
.send{background:#CFFF05;color:#0F121A;border:none;padding:10px 24px;border-radius:6px;font-weight:800;cursor:pointer;text-transform:uppercase;font-size:11px;letter-spacing:.06em}
.send:hover{background:#dfff45}
.send:disabled{background:#343E5B;color:#7a8497;cursor:not-allowed}
.empty{text-align:center;padding:60px 20px;color:#7a8497;font-size:13px}
.empty .big{font-size:28px;color:#CFFF05;margin-bottom:10px}
</style></head><body>
<header>
  <h1>Video Editor</h1>
  <div class="actions">
    <button class="actbtn" id="btn-edit" onclick="startEdit()" disabled>Start Editing</button>
    <button class="actbtn" id="btn-studio" onclick="openStudio()" disabled>Open Remotion Studio</button>
    <button class="actbtn" id="btn-tuner" onclick="openTuner()" disabled>Tuner</button>
    <button class="actbtn primary" id="btn-final" onclick="renderFinal()" disabled>Render Final</button>
  </div>
</header>
<main>
  <div class="chat" id="chat">
    <div class="empty"><div class="big">📼</div>Upload a clean video to begin.<br>I'll analyze the transcript, suggest a plan, and edit on your confirmation.</div>
  </div>
  <aside class="side">
    <div>
      <h2>Pipeline stage</h2>
      <div class="steps">
        <div class="stage" id="s-upload">Upload</div>
        <div class="stage" id="s-analyze">Analyze</div>
        <div class="stage" id="s-confirm">Confirm</div>
        <div class="stage" id="s-edit">Edit</div>
        <div class="stage" id="s-preview">Preview</div>
        <div class="stage" id="s-final">Final</div>
      </div>
    </div>
    <div class="card upload" id="dropzone">
      <div class="b">Drop clean video here</div>
      <div class="s">or paste full path</div>
      <input type="file" id="file" accept="video/*" style="display:none">
      <input type="text" id="path" placeholder="C:\Users\...\video.clean.mp4">
    </div>
    <div class="card" id="preview-card" style="display:none">
      <h2>Preview</h2>
      <video class="preview" id="player" controls></video>
    </div>
    <div class="card" id="dl-card" style="display:none">
      <h2>Final</h2>
      <a class="actbtn primary" id="dl-link" download style="display:block;text-align:center">Download .mp4</a>
    </div>
  </aside>
</main>
<footer>
  <input type="text" class="input" id="msg" placeholder="Type feedback or click Start Editing..." onkeydown="if(event.key=='Enter')send()">
  <button class="send" onclick="send()">Send</button>
</footer>
<script>
let sid = null;
const $ = id => document.getElementById(id);
const file = $('file'), path = $('path'), dz = $('dropzone'), chat = $('chat');

dz.addEventListener('click', e => { if(e.target.tagName !== 'INPUT') file.click(); });
dz.addEventListener('dragover', e => { e.preventDefault(); dz.style.borderColor='#CFFF05'; });
dz.addEventListener('dragleave', e => { dz.style.borderColor=''; });
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.style.borderColor='';
  const f = e.dataTransfer.files[0]; if(f){ path.value = f.path || f.name; startSession(); }
});
file.addEventListener('change', e => {
  const f = e.target.files[0]; if(f){ path.value = f.path || f.name; startSession(); }
});
path.addEventListener('change', startSession);

function setStage(...active) {
  ['upload','analyze','confirm','edit','preview','final'].forEach(s => $('s-'+s).classList.remove('active'));
  active.forEach(s => $('s-'+s).classList.add('active'));
}

async function startSession() {
  const p = path.value.trim(); if(!p) return;
  setStage('upload','analyze');
  chat.innerHTML = '<div class="msg status">Starting session...</div>';
  const r = await fetch('/start', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({src: p})});
  const j = await r.json();
  if(j.error){ chat.innerHTML += '<div class="msg error">'+j.error+'</div>'; return; }
  sid = j.sid;
  poll();
}

async function poll() {
  if(!sid) return;
  const r = await fetch('/chat?sid='+sid);
  const j = await r.json();
  renderChat(j.chat);
  updateUI(j);
  setTimeout(poll, 1500);
}

function renderChat(msgs) {
  if(!msgs || !msgs.length) return;
  chat.innerHTML = '';
  for (const m of msgs) {
    if(m.type === 'status'){ chat.innerHTML += '<div class="msg status">'+esc(m.text)+'</div>'; continue; }
    const cls = m.role === 'user' ? 'user' : (m.type==='transcript'?'transcript':(m.type==='error'?'error':(m.type==='done'||m.type==='final_done'?'done':'assistant')));
    chat.innerHTML += `<div class="msg ${cls}"><div class="role">${m.role}</div>${md(m.text)}</div>`;
  }
  chat.scrollTop = chat.scrollHeight;
}

function updateUI(j) {
  $('btn-edit').disabled = !(j.status === 'awaiting_approval' || j.status === 'preview_ready');
  $('btn-studio').disabled = !(j.status === 'preview_ready' || j.status === 'done');
  $('btn-tuner').disabled = !(j.status === 'preview_ready' || j.status === 'done');
  $('btn-final').disabled = !(j.status === 'preview_ready');
  if(j.status === 'preview_ready'){ setStage('preview'); if(j.preview_path){ $('preview-card').style.display='block'; $('player').src='/file?p='+encodeURIComponent(j.preview_path)+'&t='+Date.now(); } }
  if(j.status === 'done' && j.final_path){ setStage('final'); $('dl-card').style.display='block'; $('dl-link').href='/file?p='+encodeURIComponent(j.final_path); }
  if(j.status === 'editing' || j.status === 'rendering_final'){ setStage('edit'); }
  if(j.status === 'awaiting_approval'){ setStage('confirm'); }
}

function md(t) {
  return esc(t)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/_([^_]+)_/g, '<em>$1</em>')
    .replace(/\n/g, '<br>');
}
function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function send() {
  const t = $('msg').value.trim(); if(!t || !sid) return;
  $('msg').value = '';
  await fetch('/chat/send', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({sid, text: t})});
}
async function startEdit() {
  if(!sid) return;
  await fetch('/edit', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({sid})});
}
async function openStudio() {
  await fetch('/studio', {method:'POST'});
  window.open('http://localhost:3001', '_blank');
}
function openTuner() {
  window.open('http://localhost:5050', '_blank');
}
async function renderFinal() {
  if(!sid) return;
  if(!confirm('Render final 1080p (~60s)?')) return;
  await fetch('/render-final', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({sid})});
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
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/chat":
            q = parse_qs(u.query); sid = q.get("sid", [""])[0]
            sess = SESSIONS.get(sid)
            if not sess: self._json(404, {"error": "no session"}); return
            self._json(200, {
                "chat": sess.get("chat", []),
                "status": sess.get("status"),
                "preview_path": sess.get("preview_path"),
                "final_path": sess.get("final_path"),
            })
            return
        if u.path == "/file":
            q = parse_qs(u.query); p = Path(q.get("p", [""])[0])
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

        if u.path == "/start":
            src = Path(body.get("src", ""))
            if not src.exists(): self._json(400, {"error": f"file not found: {src}"}); return
            sid = uuid.uuid4().hex
            wd = workdir_for(src)
            SESSIONS[sid] = {"src": str(src), "workdir": str(wd), "status": "analyzing",
                             "chat": [], "user_notes": []}
            append_chat(sid, "assistant", f"**Loaded:** `{src.name}`. Analyzing...", "status")
            threading.Thread(target=auto_analyze, args=(sid,), daemon=True).start()
            self._json(200, {"sid": sid}); return

        if u.path == "/chat/send":
            sid = body.get("sid"); text = body.get("text", "").strip()
            if sid not in SESSIONS: self._json(404, {"error": "no session"}); return
            sess = SESSIONS[sid]
            append_chat(sid, "user", text)
            sess.setdefault("user_notes", []).append(text)
            # If plan already exists -> queue refinement prompt for Claude
            wd = Path(sess["workdir"])
            if (wd / "broll_plan.json").exists() and sess.get("status") in ("preview_ready", "done"):
                write_queue(sid, "refine_plan", text, {
                    "current_plan_path": str(wd / "broll_plan.json"),
                    "transcript_path": str(wd / "words.json"),
                })
                append_chat(sid, "assistant",
                    f"**Refinement queued.** Ping me in Claude chat — 'process the editor queue' — and I'll update the plan + re-render. Or click **Start Editing** to re-generate from scratch w/ this note included.",
                    "wait_claude")
            else:
                append_chat(sid, "assistant", "Got it. Added to plan notes. Click **Start Editing** when ready.", "msg")
            self._json(200, {"ok": True}); return

        if u.path == "/edit":
            sid = body.get("sid")
            if sid not in SESSIONS: self._json(404, {"error": "no session"}); return
            threading.Thread(target=run_edit, args=(sid,), daemon=True).start()
            self._json(200, {"ok": True}); return

        if u.path == "/render-final":
            sid = body.get("sid")
            if sid not in SESSIONS: self._json(404, {"error": "no session"}); return
            threading.Thread(target=final_render, args=(sid,), daemon=True).start()
            self._json(200, {"ok": True}); return

        if u.path == "/studio":
            launch_studio()
            self._json(200, {"ok": True}); return

        self.send_response(404); self.end_headers()


if __name__ == "__main__":
    port = 5002
    print(f"Video Editor: http://localhost:{port}")
    HTTPServer(("localhost", port), H).serve_forever()
