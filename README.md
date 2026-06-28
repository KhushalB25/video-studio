# 🎬 Video Studio

A local, open-source video editor for short-form content (9:16 / 16:9) — runs entirely on your machine. Drop a raw video, the studio cleans it (silence trim, retake removal, audio denoise), generates a beat plan (captions, animated overlays, brand logos, music), renders a preview, and lets you tune every placement before exporting a final 1080p file.

Inspired by Descript-style transcript editing + CapCut-style timeline editing, packaged as a single localhost web app you can run on any laptop with Python + Node + ffmpeg.

---

## ✨ Features

### Phase 1 — Clean
- **Multi-clip combine** — drop multiple raw clips, reorder, combine into one cleaned footage
- **Silence detection** (ffmpeg `silencedetect`) w/ configurable threshold + min gap
- **Gap shortening** — replace long pauses with a target gap (e.g. 0.30s)
- **Retake removal** — sentence-level `SequenceMatcher` heuristic detects repeated phrases and keeps the last take, cutting just the matched span (not the whole sentence)
- **Click / pop detection** — short transients flagged + removed
- **Background noise removal** — RNNoise + spectral `afftdn`, intensity slider 0–1
- **Studio sound enhance** — EQ + compress + loudness normalize, intensity slider 0–1
- **Transcript editor** — click any word in the transcript to mark for removal; click again to restore
- **Waveform visualization** — canvas plot w/ red silence-band overlays
- **Undo last cut** — full session history stack
- **Head / tail silence toggles** — explicit ON/OFF switches

### Phase 2 — Edit (overlays + music + effects)
- **Auto-plan** — instant rule-based beat plan from transcript heuristics (no LLM call required)
- **Plan templates** — Hook + Stats + CTA / Story arc / Tutorial steps / Product demo
- **Beat timeline editor** — colored draggable blocks (one color per beat kind). Drag body to move, drag edges to resize, click to select
- **Per-beat inline editor** — every field of every kind editable: kicker, title, value, caption, items (JSON), bars, vertical, card_top, etc.
- **Beat add / delete / duplicate** — dropdown of every kind, one-click insertion
- **Asset upload** — drag image directly onto a beat to set image_path
- **Brand logo auto-fetch** — type "Stripe" → fetches from Wikipedia → preview → assign to beat
- **Pexels stock search** — search query → grid of thumbnails → click to download + assign
- **Music picker w/ preview** — every .mp3 in `assets/` listed w/ inline audio player
- **Undo / Redo** — `Ctrl+Z` / `Ctrl+Y`, plan history stack 30 deep
- **27 Remotion overlay templates**: hook_title, word_pop, stat_punch, quote_pull, image_card, tool_logo_burst, portrait_burst, bullet_burst, bar_overlay, vertical_timeline, horizontal_timeline, cinematic_title, chapter_bar, title_card, keyword_chips, progress_steps, ratio_dots, agent_avatar_burst, inline_chart, dashboard_card, claude_code_terminal, org_diagram, headline_card, subscribe, captions, etc.
- **Auto-burned captions** from WhisperX transcript w/ per-line bottom-offset override
- **Speaker layer effects**: follow-cam (motion tracking), zoom punch-ins, color grade, follow-zoom

### Phase 3 — Export
- **4 quality presets**: Draft (720p / 30fps / 2Mbps), Standard (1080p / 30fps / 6Mbps), High (1080p / 60fps / 10Mbps), Pro (4K / 30fps / 20Mbps H.265)
- **Custom mode**: each field individually editable
- **Music + SFX baked in** via `score.sh` audio pipeline (sidechain ducking under voice, climax swell envelope)

### Cross-cutting
- **🌓 Dark / Light theme** toggle, persists in localStorage
- **Project save / load** — `<workdir>/project.studio.json` w/ full state. Auto-saves every 30s
- **Projects dashboard** — grid of past projects w/ thumbnails. Click to reopen / resume
- **⚠ Error log panel** — collected from all subprocess calls, timestamp + source + message
- **Help tour modal** on first run
- **Keyboard shortcuts**: Space (play/pause), J/K/L (scrub), Ctrl+Z/Y (undo/redo), Ctrl+S (save), 1/2/3 (switch tabs)
- **Position Tuner** (separate page on :5050) — per-beat + per-caption vertical placement sliders w/ live numeric input
- **Remotion Studio integration** — launch button opens :3001 for HMR template development

---

## 🖥️ Architecture

```
┌─ Browser (HTML/CSS/JS — zero build step) ───────────────────┐
│   Projects · Clean · Edit · Export  (4 tabs)                 │
└────────────────────────────────────────────────────────────┘
        ↑ HTTP / WebSocket
┌─ Python Flask backend (port 5000) ─────────────────────────┐
│   • Pipeline orchestration                                  │
│   • Session state on disk (resumable)                       │
│   • Prompt queue (file-based, optional LLM integration)     │
└────────────────────────────────────────────────────────────┘
        ↓ subprocess
   ┌────────────────┬───────────────────┬─────────────────┐
   │ ffmpeg         │ WhisperX (venv)   │ Remotion (npx)  │
   │ silence, splice│ word-level STT    │ overlay render  │
   │ denoise, mux   │                   │                 │
   └────────────────┴───────────────────┴─────────────────┘
                                                ↓
                              Optional services on demand
                              ┌──────────────────┬──────────┐
                              │ Pexels API       │ Wikipedia│
                              │ (stock photos)   │(logos)   │
                              └──────────────────┴──────────┘
```

**No build step for the frontend.** It's vanilla HTML + CSS + JS served from Flask static. Edit `studio.py` and refresh.

---

## 📋 System Requirements

| Tool | Min version | Why |
|---|---|---|
| Python | 3.10+ | Studio backend, WhisperX |
| Node.js | 18+ | Remotion render pipeline |
| ffmpeg | 6+ | Silence detect, splice, audio filters, encode |
| Git Bash (Windows) | any | render.sh + score.sh are bash |
| Chrome / Edge / Firefox | any modern | UI (no IE) |

**RAM**: 8 GB minimum, 16 GB recommended for WhisperX + Remotion concurrently.
**Disk**: ~5 GB for installed deps + space for your videos.
**OS**: tested on Windows 11. Should work on macOS/Linux with minor path tweaks (some scripts use Windows-specific path normalization — see `studio.py` `copy_to_render_wd`).

---

## 🚀 Installation

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/video-studio.git
cd video-studio
```

### 2. Install system tools

**Windows (winget):**
```powershell
winget install Gyan.FFmpeg
winget install OpenJS.NodeJS.LTS
winget install Python.Python.3.10
```

**macOS (Homebrew):**
```bash
brew install ffmpeg node@18 python@3.10
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt update && sudo apt install -y ffmpeg nodejs npm python3 python3-venv python3-pip
```

Verify each:
```bash
ffmpeg -version
node --version
python --version
```

### 3. Python venv + WhisperX

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -U pip
pip install -r requirements.txt
pip install whisperx
# whisperx pulls torch (~2GB), faster-whisper, ctranslate2, pyannote, etc.
# CPU build is fine; GPU optional.
```

First run of `transcribe.py` will download the WhisperX `base` model (~360 MB). Cached in `~/.cache/whisper/`.

### 4. Install Remotion dependencies

```bash
cd remotion
npm install
cd ..
```

This pulls Remotion 4.0.453, React 18, esbuild. ~500 MB total.

### 5. Configure API keys (optional but recommended)

```bash
cp .env.example .env
```

Edit `.env`:
- **PEXELS_API_KEY** — stock photo search inside the editor. Free at https://www.pexels.com/api/. Without this, only manual image upload works.
- **ANTHROPIC_API_KEY** — LLM-powered retake detection + smart plan authoring. Free credits at https://console.anthropic.com/. Without this, falls back to rule-based heuristics (worse quality but works offline).

### 6. Verify ffmpeg + whisperx are wired

```bash
python scripts/transcribe.py "path/to/test_video.mp4"
# should write words.json to ~/.cache/video-edit/<videoname>_<hash>/
```

### 7. Start the studio

```bash
python scripts/studio.py
# → http://localhost:5000
```

Optional companion services:
```bash
# Position Tuner — slider GUI for placement
python scripts/position_tuner.py
# → http://localhost:5050

# Remotion Studio — live HMR for template development
cd remotion && npx remotion studio src/index.ts --port 3001
# → http://localhost:3001
```

The studio's header has buttons to launch both companions on demand.

---

## 📁 Repo Structure

```
video-studio/
├── README.md                       (this file)
├── LICENSE                         (MIT)
├── SKILL.md                        ★ The 100+ authoring rules — READ THIS if customizing
├── requirements.txt                Python deps
├── .env.example                    Template for API keys
├── .gitignore
│
├── scripts/
│   ├── studio.py                   ★ MAIN — unified studio web app (port 5000)
│   ├── position_tuner.py           Slider GUI (port 5050)
│   ├── cleaner_app.py              Standalone Phase 1 cleaner (legacy, kept for reference)
│   ├── editor_app.py               Standalone Phase 2 editor (legacy)
│   │
│   ├── transcribe.py               WhisperX wrapper → words.json
│   ├── render.sh                   Remotion render pipeline (preview + final)
│   ├── score.sh                    Audio mix (music + SFX + voice ducking + swell)
│   ├── build_sfx_track.py          Generate SFX timed to beats
│   ├── lint_plan.py                Plan schema validator (per-kind required fields)
│   ├── align_to_speech.py          Snap beat boundaries to spoken words
│   ├── sync_list_items.py          Extend list end_sec to cover items + dwell
│   ├── close_gaps.py               Bridge beats with <0.6s gap (no flicker frames)
│   ├── captions_plan.py            Build burned-in caption track from words.json
│   ├── polish_transcript.py        Re-correct transcript against audio (uses Anthropic API)
│   ├── align_transcript_to_script.py  Match transcript to canonical script (brand names)
│   ├── fetch_logo.py               Wikipedia brand logo fetcher (idempotent, scored)
│   ├── fetch_stock.py              Pexels stock photo/video fetcher
│   ├── zoom_plan.py                Speaker punch-in plan (sentence-start heuristic)
│   ├── build_followcam.py          Per-frame pan+zoom track from speaker matte
│   ├── segment_speaker.py          rembg matte for behind_subject text
│   ├── extract_stills.sh           Per-beat contact sheet for QA
│   ├── verify.py                   Post-render duration/audio bit-identity check
│   └── _envloader.py               .env reader (used by Python scripts)
│
├── remotion/
│   ├── package.json
│   ├── tsconfig.json
│   └── src/
│       ├── index.ts                Remotion entry
│       ├── Root.tsx                Composition registry
│       ├── EditedVideo.tsx         ★ Main 9:16 composition (speaker + b-roll + captions)
│       └── templates/
│           ├── HookTitle.tsx           Big cold-open lockup
│           ├── WordPop.tsx             Cardless typography overlay
│           ├── StatPunch.tsx           Hero number takeover
│           ├── QuotePull.tsx           Typewriter quote
│           ├── ImageCard.tsx           Glassy bottom-half image card
│           ├── ToolLogoBurst.tsx       Multi-brand scattered logos
│           ├── PortraitBurst.tsx       Circular face thumbs
│           ├── BulletBurst.tsx         Accumulating cardless bullets
│           ├── BarOverlay.tsx          Lower-third bar chart
│           ├── VerticalTimeline.tsx    Rail-drives-the-dots step viz
│           ├── HorizontalTimeline.tsx
│           ├── CinematicTitle.tsx      Chapter + title + subtitle takeover
│           ├── ChapterBar.tsx          Bottom-third chapter banner
│           ├── TitleCard.tsx           Number + title
│           ├── KeywordChips.tsx        Pills of keywords
│           ├── ProgressSteps.tsx       Step bar w/ progress
│           ├── RatioDots.tsx           X-of-Y dot grid
│           ├── AgentAvatarBurst.tsx    Robot-face thumbs
│           ├── InlineChart.tsx         Line graph overlay
│           ├── DashboardCard.tsx       Mock SaaS dashboard
│           ├── ClaudeCodeTerminal.tsx  Mac-window terminal w/ typewriter
│           ├── OrgDiagram.tsx          12-box org chart w/ progressive reveal
│           ├── HeadlineCard.tsx        News-clipping card
│           ├── SubscribeButton.tsx     Animated subscribe pill
│           ├── Captions.tsx            Word-punch captions
│           ├── Backgrounds.tsx         Cloudy grids
│           ├── ColorGrade.tsx          Cinematic LUT-like grade
│           └── motion.ts               Shared springs/easings
│
├── assets/
│   ├── logos/                      Brand library (.png — Anthropic, OpenAI, Chrome, etc.)
│   └── *.mp3                       Music tracks (royalty-free)
│
└── knowledge/                      Authoring reference docs
    ├── intro_recipe.md             5-min recipe for intro videos
    ├── longform_workflow.md        Per-chapter pass for longform
    ├── template_library.md         Visual reference of every kind
    └── image_style.md              Locked AI-image prompt style
```

---

## 🛠️ Quick Start (after install)

1. Run `python scripts/studio.py`
2. Open `http://localhost:5000`
3. **Clean tab** — drop one or more raw `.mp4` clips. Adjust silence threshold + gap sliders. Toggle ON/OFF: head silence, tail silence, retakes, clicks. Adjust denoise + studio sound intensities. Click **Clean video** → outputs `<name>.clean.mp4`.
4. **Edit tab** — click **Auto-edit** for an instant beat plan (or pick a template). Watch the timeline populate. Click any beat to edit. Drag to move/resize. Type prompts in the chat box for refinements. Click **Re-render preview** to see the result.
5. **Open Remotion Studio + Tuner** — opens companion apps for live template preview + slider-based placement tuning.
6. **Export tab** — pick quality preset → render final → download from `~/Downloads/`.

---

## 🎯 Use Cases

- YouTube Shorts / Instagram Reels / TikTok creators producing 30–60s explainer content
- Solo developers documenting builds w/ talking-head + screen-recording footage
- Podcast clip repurposing (extract single-speaker segments, add overlays)
- Educational content w/ heavy use of stats, lists, comparisons

NOT optimized for: long-form films, multi-speaker dialog scenes, music videos, anything requiring color grading beyond a basic cinematic LUT.

---

## 🔌 Optional Integrations

| Integration | Purpose | Required key |
|---|---|---|
| Pexels API | Stock photo / video search | `PEXELS_API_KEY` |
| Anthropic API | LLM retake detection, smart plan authoring, per-beat prompt edits | `ANTHROPIC_API_KEY` |
| OpenAI Whisper API | Alternative to local WhisperX (faster, paid) | Not wired by default — uncomment in `transcribe.py` |
| Higgsfield `gpt_image_2` | AI-generated b-roll for abstract metaphors | MCP server only (Claude Code) |
| Local Ollama | Offline LLM fallback for plan authoring | Not wired — see `studio.py` `generate_rule_plan` for hook |

Skip any of these — the studio works without them, falling back to deterministic heuristics.

---

## 🐛 Troubleshooting

**`whisperx` import fails** → check you ran `pip install whisperx` *inside* the venv. WhisperX pulls torch, which may need C++ build tools on Windows.

**Render exits with non-zero** → click the ⚠ badge in studio header → see the captured ffmpeg stderr. Common causes: stale render lock (auto-cleared on next attempt), corrupted props.json (delete `remotion/src/props.json` and re-render), missing asset file (check `broll_plan.json` paths).

**Caption mojibake (`â€"`)** → JSON encoding mismatch. Windows reads files as cp1252 by default. Studio writes all JSON with `ensure_ascii=True`. If you hand-edit a JSON file with em-dashes, save as UTF-8 BOM or escape them as `—`.

**Overlay lands on the speaker's face** → CSS `padding-top: %` resolves against container WIDTH, not height. Every template's positioning uses pixel padding (`Math.round(vertical * height) + 'px'`). If you add a new template, follow this convention.

**Studio Sound doesn't match Descript quality** → it doesn't. Their model is proprietary. This pipeline uses RNNoise + ffmpeg `afftdn` + `dynaudnorm` + EQ + compressor — good for clean talking heads, not magazine-grade.

**Two renders fight for the lock** → render.sh serializes via `/tmp/video-edit-render.lock.d`. Studio pre-clears stale locks before each render. If both Remotion Studio (port 3001) and a render are running concurrently, the studio's HMR webpack cache fights the render's `rm -rf node_modules/.cache`. Stop studio before triggering a render.

---

## 🤝 Contributing

PRs welcome! Focus areas:

- **More overlay templates** — see existing TSX files for patterns. Add to `Root.tsx`'s comp registry + extend `studio.py`'s `DEFAULT_BEAT_FIELDS` + `add-kind` dropdown.
- **Better retake detection** — current uses sentence-level `SequenceMatcher`. Could improve w/ semantic similarity (sentence embeddings).
- **macOS / Linux path fixes** — `studio.py` has Windows-specific path normalization. PRs to clean up cross-platform.
- **Drag-to-position overlay** — current tuner is slider-only. A click-and-drag overlay on the live preview would be powerful.
- **Direct platform uploads** — YouTube / Instagram / TikTok upload integrations.
- **Local LLM integration** — wire Ollama as the LLM provider for retake detection + plan authoring.

Read `SKILL.md` first — it documents 100+ enforced authoring rules (beat density caps, vertical placement rules, caption suppression logic, etc.). Don't break them.

---

## 📜 License

MIT — see [LICENSE](LICENSE).

---

## 🙏 Credits

- Built atop [Remotion](https://www.remotion.dev/) for programmatic React-based video rendering
- [WhisperX](https://github.com/m-bain/whisperX) for word-level transcription
- [ffmpeg](https://ffmpeg.org/) for everything video / audio
- [Pexels](https://www.pexels.com/) for free stock media
- Background-noise removal via [RNNoise](https://github.com/xiph/rnnoise)
- Brand logos sourced from Wikipedia's `pageimages` API
- Inspired by [Descript](https://www.descript.com/) (transcript editing) and [CapCut](https://www.capcut.com/) (timeline editing)
