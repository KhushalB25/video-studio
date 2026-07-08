# Video Studio

A local video editor that edits itself. Drop in a raw talking-head clip, open the web UI, and it transcribes, cuts silence, stabilizes, follows your face, composites captions and motion graphics, and scores music/SFX — entirely on your own machine.

**Nothing gets uploaded.** The video never leaves your computer and no third-party editing service is involved. Claude is the only outside intelligence in the loop, and it never touches the video file itself — it reads a transcript and writes a small JSON edit plan; the local pipeline (ffmpeg, WhisperX, OpenCV, MediaPipe, Remotion) does the actual rendering.

## Table of contents

- [How it works](#how-it-works)
- [Tech stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Running it](#running-it)
- [Project structure](#project-structure)
- [Configuration](#configuration)
- [Why local-only](#why-local-only)

## How it works

1. **Drop a video** into the Studio UI. It's transcribed and rough-cut locally.
2. **Tell it what you want**, in plain English, in the prompt queue panel — "add more b-roll in the middle", "use a different hook", etc.
3. **Claude reads the transcript + your prompt** and writes/updates an edit plan (captions, b-roll beats, camera moves, callouts, subscribe CTA, and so on) as JSON.
4. **The pipeline renders it**: ffmpeg cuts/stabilizes/moves the camera, OpenCV + MediaPipe drive face-follow, Remotion composites captions and graphics, and the audio gets scored with music + SFX.
5. **Iterate**: tweak the plan, re-render, export when it looks right. Every stage caches on its own inputs, so a re-render only redoes what actually changed.

## Tech stack

| Layer | Technology | Used for |
|---|---|---|
| Web app | Python 3 standard library (`http.server`, `ThreadingHTTPServer`) | The Studio UI/API server — no web framework, no external dependency |
| Transcription | [WhisperX](https://github.com/m-bain/whisperX) | Word-level timestamps + forced alignment, run locally |
| Video I/O & processing | [ffmpeg](https://ffmpeg.org/) | Cutting, splicing, stabilizing, audio filtering, muxing |
| Camera movement | [OpenCV](https://opencv.org/) (`cv2`) | Frame-by-frame crop/zoom for dynamic camera moves |
| Face tracking | [MediaPipe](https://developers.google.com/mediapipe) | Face-follow camera mode |
| Speaker cutout | [rembg](https://github.com/danielgatis/rembg), [PyAV](https://github.com/PyAV-Org/PyAV) | Background removal for "behind subject" overlay beats |
| Image/array processing | NumPy, Pillow | Follow-cam track math, image compositing |
| Compositing/rendering | [Remotion](https://www.remotion.dev/) (React + TypeScript, Node.js) | Captions, template overlays, b-roll, final video render |
| AI decision-making | Claude (via Claude Code) | Reads the transcript + your prompt, authors/edits the JSON plan |

## Prerequisites

- **Python 3** with `pip`
- **Node.js 18+** with `npm` (for Remotion)
- **ffmpeg** on your `PATH`
- **Claude Code**, running locally, to author edit plans from the prompt queue

## Installation

**macOS / Linux:**
```bash
git clone <this-repo>
cd video-studio
bash scripts/setup.sh
```

**Windows:**
```bat
git clone <this-repo>
cd video-studio
scripts\setup.bat
```

Either script will:
1. Check for `ffmpeg` on your `PATH`
2. Create a Python virtual environment (`.venv`) and install Python dependencies (WhisperX, OpenCV, MediaPipe, NumPy, Pillow, rembg, PyAV)
3. Run `npm install` inside `remotion/` to install the Remotion renderer

If you'd rather install the Python side by hand:
```bash
python3 -m venv .venv
source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install whisperx opencv-python mediapipe numpy pillow rembg av
cd remotion && npm install
```

## Running it

```bash
python3 scripts/studio.py
```

Open `http://localhost:5056` (falls back from 5000, which macOS reserves for AirPlay). Tabs: **Clean** (trim/stabilize/camera) → **Edit** (captions, b-roll, beats) → **Export** (final render + download) → **Projects**.

## Project structure

```
scripts/    Python server + all pipeline steps (transcribe, splice, stabilize, camera, render, ...)
remotion/   React/TypeScript compositing engine — captions, overlay templates, final render
knowledge/  Editing rules, template library, music/SFX reference the plan-authoring step follows
assets/     Brand assets used in renders (logos, subscribe bug)
```

## Configuration

Environment variables the scripts read, all optional:

| Variable | Purpose |
|---|---|
| `PEXELS_API_KEY` | Stock-footage fallback when no real screenshot/asset exists ([free key](https://www.pexels.com/api/)) |
| `WHISPER_MODEL` | WhisperX model size (defaults to `base`) |
| `STUDIO_PORT` | Port for the Studio web UI (defaults to `5056`) |

No other API keys are required.

## Why local-only

Talking-head video is personal. This tool was built so editing it never means handing it to a hosted service: everything lives in a local cache folder on disk, every render happens on your own CPU/GPU, and the only "AI" involved is Claude deciding *what* to place where — not a cloud pipeline processing your footage.
