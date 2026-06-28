"""Position tuner — http://localhost:5050
Every beat + every caption individually editable. Group-set per type.
Slider + text input. Writes to props.json + captions_plan.json + Captions.tsx default.
Studio (3001) HMR-reloads on each change.
"""
from __future__ import annotations
import json, re, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
PROPS = SKILL / "remotion/src/props.json"
CAP_TSX = SKILL / "remotion/src/templates/Captions.tsx"

WORKDIR = Path(r"C:/Users/DELL/.cache/video-edit/WhatsApp Video 2026-06-27_95d5f8227b38")
PLAN = WORKDIR / "broll_plan.json"
SOURCE = WORKDIR / "broll_plan.source.json"
CAPS = WORKDIR / "captions_plan.json"
JOB_CAPS_GLOB = list((SKILL / "remotion/public").glob("job-*/captions_plan.json"))

VERTICAL_KINDS = {"hook_title", "word_pop", "subscribe", "bar_overlay",
                  "tool_logo_burst", "portrait_burst", "bullet_burst",
                  "ratio_dots", "agent_avatar_burst", "inline_chart"}
IMAGE_CARD_KINDS = {"image_card"}


def load_props():
    if not PROPS.exists(): return []
    d = json.loads(PROPS.read_text(encoding="utf-8"))
    out = []
    def walk(x):
        if isinstance(x, list):
            for i in x: walk(i)
        elif isinstance(x, dict):
            if x.get("kind") in VERTICAL_KINDS or x.get("kind") in IMAGE_CARD_KINDS:
                out.append(x)
            for v in x.values(): walk(v)
    walk(d)
    out.sort(key=lambda b: b.get("start_sec", 0))
    return out


def load_caps():
    if not CAPS.exists(): return []
    return json.loads(CAPS.read_text(encoding="utf-8"))


def write_beat(idx: int, field: str, value: float):
    """Update beat at index in BOTH props.json AND plan/source — keyed by start_sec match."""
    beats = load_props()
    if idx >= len(beats): return
    target_start = beats[idx].get("start_sec")
    target_kind = beats[idx].get("kind")
    for p in [PROPS, PLAN, SOURCE]:
        if not p.exists(): continue
        d = json.loads(p.read_text(encoding="utf-8"))
        def walk(x):
            if isinstance(x, list):
                for i in x: walk(i)
            elif isinstance(x, dict):
                if x.get("kind") == target_kind and abs(x.get("start_sec", -999) - target_start) < 0.01:
                    x[field] = round(value, 3) if isinstance(value, float) else value
                    # also need to ASCII-only output for non-string values
                    pass
                for v in x.values(): walk(v)
        walk(d)
        p.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="ascii")


def write_group(kind: str, field: str, value: float):
    for p in [PROPS, PLAN, SOURCE]:
        if not p.exists(): continue
        d = json.loads(p.read_text(encoding="utf-8"))
        def walk(x):
            if isinstance(x, list):
                for i in x: walk(i)
            elif isinstance(x, dict):
                if x.get("kind") == kind:
                    x[field] = round(value, 3) if isinstance(value, float) else value
                for v in x.values(): walk(v)
        walk(d)
        p.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="ascii")


def write_caption_default(bottom_frac: float):
    text = CAP_TSX.read_text(encoding="utf-8")
    new = re.sub(
        r"(line\.bottom_offset \?\? \(isLandscape \? [\d.]+ : )[\d.]+(\))",
        rf"\g<1>{round(bottom_frac, 3)}\g<2>",
        text,
    )
    CAP_TSX.write_text(new, encoding="utf-8")


def _update_props_captions(mut):
    if not PROPS.exists(): return
    d = json.loads(PROPS.read_text(encoding="utf-8"))
    if isinstance(d, dict) and isinstance(d.get("captions"), list):
        mut(d["captions"])
        PROPS.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="ascii")


def write_caption_line(idx: int, bottom_frac: float):
    for cp in [CAPS] + JOB_CAPS_GLOB:
        if not cp.exists(): continue
        d = json.loads(cp.read_text(encoding="utf-8"))
        if idx < len(d):
            d[idx]["bottom_offset"] = round(bottom_frac, 3)
        cp.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="ascii")
    def mut(caps):
        if idx < len(caps):
            caps[idx]["bottom_offset"] = round(bottom_frac, 3)
    _update_props_captions(mut)


def write_caption_group(bottom_frac: float):
    for cp in [CAPS] + JOB_CAPS_GLOB:
        if not cp.exists(): continue
        d = json.loads(cp.read_text(encoding="utf-8"))
        for line in d:
            line["bottom_offset"] = round(bottom_frac, 3)
        cp.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="ascii")
    def mut(caps):
        for line in caps:
            line["bottom_offset"] = round(bottom_frac, 3)
    _update_props_captions(mut)


def read_default_caption_offset():
    text = CAP_TSX.read_text(encoding="utf-8")
    m = re.search(r"line\.bottom_offset \?\? \(isLandscape \? [\d.]+ : ([\d.]+)\)", text)
    return float(m.group(1)) if m else 0.11


HTML_HEAD = """<!doctype html><html><head><meta charset="utf-8"><title>Position Tuner</title>
<style>
  *{box-sizing:border-box}
  body{font-family:-apple-system,system-ui,sans-serif;background:#0F121A;color:#E9ECED;margin:0;padding:20px}
  h1{color:#CFFF05;font-size:14px;text-transform:uppercase;letter-spacing:.08em;margin:0 0 4px}
  .sub{color:#B5BFC2;font-size:11px;margin-bottom:16px}
  .section{background:#1E2434;border-radius:8px;padding:14px;margin-bottom:14px}
  .section h2{margin:0 0 10px;font-size:12px;color:#CFFF05;text-transform:uppercase;letter-spacing:.05em}
  .row{display:grid;grid-template-columns:170px 1fr 60px;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid rgba(255,255,255,.03)}
  .row:last-child{border-bottom:none}
  .label{font-size:12px;color:#D2D8DA}
  .label .small{color:#B5BFC2;font-size:10px;display:block;margin-top:2px}
  input[type=range]{width:100%;accent-color:#CFFF05}
  input[type=number]{width:60px;background:#0F121A;border:1px solid #343E5B;color:#E9ECED;padding:4px 6px;border-radius:4px;font-family:monospace;font-size:11px}
  input[type=number]:focus{outline:1px solid #CFFF05}
  .group{background:#343E5B;padding:8px 10px;border-radius:6px;margin-bottom:10px;display:grid;grid-template-columns:170px 1fr 60px;gap:10px;align-items:center}
  .group .label{color:#CFFF05;font-size:11px;text-transform:uppercase;font-weight:600}
  details summary{cursor:pointer;color:#B5BFC2;font-size:11px;padding:4px 0;user-select:none}
  details summary:hover{color:#CFFF05}
  details[open] summary{margin-bottom:8px}
  .pill{display:inline-block;background:#343E5B;color:#CFFF05;padding:1px 6px;border-radius:3px;font-size:9px;margin-right:6px;font-family:monospace}
  .col{max-width:760px;margin:0 auto}
</style></head><body><div class="col">
<h1>Position Tuner</h1>
<div class="sub">Slider OR type number. Saves instantly. Hard-refresh Studio (Ctrl+Shift+R) on <a href="http://localhost:3001" style="color:#CFFF05">localhost:3001</a> to see.<br>0 = top of frame, 1 = bottom. Face roughly y 0.13–0.47.</div>
"""

def fmt_field_row(label, key, value, mn, mx, step, hint=""):
    return f"""
    <div class="row">
      <div class="label">{label}{f'<span class="small">{hint}</span>' if hint else ''}</div>
      <input type="range" min="{mn}" max="{mx}" step="{step}" value="{value}"
             oninput="document.getElementById('n-{key}').value=this.value; save('{key}', this.value)"/>
      <input type="number" id="n-{key}" min="{mn}" max="{mx}" step="{step}" value="{value}"
             oninput="save('{key}', this.value)"/>
    </div>
    """


def render_page():
    beats = load_props()
    caps = load_caps()
    cap_default = read_default_caption_offset()

    html = HTML_HEAD

    # group beats by kind
    by_kind = {}
    for i, b in enumerate(beats):
        by_kind.setdefault(b.get("kind"), []).append((i, b))

    # group controls
    html += '<div class="section"><h2>Group set (apply to all of kind)</h2>'
    for kind in sorted(by_kind):
        if kind in VERTICAL_KINDS:
            # use first beat's vertical or default
            v = by_kind[kind][0][1].get("vertical", 0.5)
            html += f"""
            <div class="group">
              <div class="label">{kind} (x{len(by_kind[kind])})</div>
              <input type="range" min="0" max="1" step="0.01" value="{v}"
                     oninput="document.getElementById('gn-{kind}').value=this.value; saveGroup('{kind}','vertical',this.value)"/>
              <input type="number" id="gn-{kind}" min="0" max="1" step="0.01" value="{v}"
                     oninput="saveGroup('{kind}','vertical',this.value)"/>
            </div>"""
        elif kind == "image_card":
            for i, b in by_kind[kind][:1]:
                pass  # image_card uses card_top not vertical
    html += '</div>'

    # captions group
    html += f'<div class="section"><h2>Captions — default bottom offset</h2>'
    html += f"""
    <div class="group">
      <div class="label">all captions (x{len(caps)})</div>
      <input type="range" min="0" max="0.5" step="0.005" value="{cap_default}"
             oninput="document.getElementById('cap-default').value=this.value; saveCapGroup(this.value)"/>
      <input type="number" id="cap-default" min="0" max="0.5" step="0.005" value="{cap_default}"
             oninput="saveCapGroup(this.value)"/>
    </div>
    <div style="font-size:10px;color:#B5BFC2;margin-top:4px">Larger value = caption sits higher above bottom edge. Per-line overrides below take precedence.</div>
    </div>"""

    # individual beats
    html += '<div class="section"><h2>Individual beats</h2>'
    for kind, items in sorted(by_kind.items()):
        html += f'<details {"open" if kind in ("hook_title","subscribe","bar_overlay","image_card") else ""}><summary>{kind} ({len(items)})</summary>'
        for i, b in items:
            label = f'<span class="pill">{b.get("start_sec",0):.1f}s</span>'
            # title/text snippet for context
            snippet = b.get("title") or b.get("caption") or ""
            if not snippet and b.get("items"):
                snippet = (b["items"][0].get("text") or "")[:24]
            label += snippet[:30]
            if kind in VERTICAL_KINDS:
                v = b.get("vertical", 0.5)
                html += fmt_field_row(label, f"beat-{i}-vertical", v, 0, 1, 0.01)
            elif kind == "image_card":
                ct = b.get("card_top", 0.60)
                cb = b.get("card_bottom", 0.04)
                cm = b.get("card_margin", 0.06)
                isc = b.get("image_scale", 1.0)
                html += fmt_field_row(label + " (card_top)", f"beat-{i}-card_top", ct, 0, 1, 0.01, "top edge of card")
                html += fmt_field_row("↳ card_bottom", f"beat-{i}-card_bottom", cb, 0, 0.5, 0.01, "gap from frame bottom")
                html += fmt_field_row("↳ card_margin", f"beat-{i}-card_margin", cm, 0, 0.3, 0.01, "horizontal margin (L/R)")
                html += fmt_field_row("↳ image_scale", f"beat-{i}-image_scale", isc, 0.3, 2.0, 0.05, "scale image inside card")
                fit = b.get("card_fit", "wide")
                html += f"""
                <div class="row">
                  <div class="label">↳ card_fit</div>
                  <select onchange="saveFit({i}, this.value)" style="background:#0F121A;color:#E9ECED;border:1px solid #343E5B;padding:4px 6px;border-radius:4px;font-size:11px">
                    <option value="wide" {'selected' if fit=='wide' else ''}>wide (full-width, image contained)</option>
                    <option value="hug" {'selected' if fit=='hug' else ''}>hug (card shrinks to image aspect)</option>
                  </select>
                  <span></span>
                </div>"""
        html += '</details>'
    html += '</div>'

    # individual captions
    html += f'<div class="section"><h2>Individual captions ({len(caps)})</h2><details><summary>Expand all caption lines</summary>'
    for i, c in enumerate(caps):
        words = " ".join(w["text"] for w in c.get("words", []))[:32]
        label = f'<span class="pill">{c.get("start_sec",0):.1f}s</span>{words}'
        v = c.get("bottom_offset", cap_default)
        html += fmt_field_row(label, f"cap-{i}-bottom_offset", v, 0, 0.5, 0.005)
    html += '</details></div>'

    html += """
    <script>
    function save(key, val){
      if(key.startsWith('cap-')){
        const idx = key.split('-')[1];
        fetch('/set?kind=cap_line&idx='+idx+'&value='+val);
      } else {
        const parts = key.split('-');
        const idx = parts[1];
        const field = parts.slice(2).join('-');
        fetch('/set?kind=beat&idx='+idx+'&field='+field+'&value='+val);
      }
    }
    function saveGroup(kind, field, val){
      fetch('/set?kind=group&group_kind='+kind+'&field='+field+'&value='+val);
    }
    function saveCapGroup(val){
      fetch('/set?kind=cap_group&value='+val);
    }
    function saveFit(idx, val){
      fetch('/set?kind=beat_str&idx='+idx+'&field=card_fit&value='+val);
    }
    function saveCap(key, val){
      const [_, idx] = key.split('-');
      fetch('/set?kind=cap_line&idx='+idx+'&value='+val);
    }
    // wire cap line saves
    document.addEventListener('input', e=>{
      const id = e.target.id||'';
      if(id.startsWith('n-cap-')){ saveCap(id.slice(2), e.target.value); }
    }, true);
    document.querySelectorAll('input[type=range]').forEach(r=>{
      const id = r.nextElementSibling && r.nextElementSibling.id;
      if(id && id.startsWith('n-cap-')){
        r.addEventListener('input',()=>saveCap(id.slice(2), r.value));
      }
    });
    </script></div></body></html>
    """
    return html


class H(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            body = render_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/set":
            kind = q.get("kind", [""])[0]
            try:
                if kind == "beat":
                    write_beat(int(q["idx"][0]), q["field"][0], float(q["value"][0]))
                elif kind == "beat_str":
                    write_beat(int(q["idx"][0]), q["field"][0], q["value"][0])
                elif kind == "group":
                    write_group(q["group_kind"][0], q["field"][0], float(q["value"][0]))
                elif kind == "cap_group":
                    write_caption_group(float(q["value"][0]))
                    write_caption_default(float(q["value"][0]))
                elif kind == "cap_line":
                    write_caption_line(int(q["idx"][0]), float(q["value"][0]))
            except Exception as e:
                self.send_response(500); self.end_headers(); self.wfile.write(str(e).encode()); return
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
        self.send_response(404); self.end_headers()


if __name__ == "__main__":
    print("Position tuner: http://localhost:5050")
    HTTPServer(("localhost", 5050), H).serve_forever()
