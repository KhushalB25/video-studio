# Video Studio

A local video editor that edits itself. Drop in a raw talking-head video, open the web UI, and it transcribes, cuts silence, stabilizes, tracks your face, adds captions and motion graphics, and scores music/SFX — all on your own machine.

**Nothing gets uploaded.** The video never leaves your computer, and no third-party editing service is involved. Claude is the only outside intelligence in the loop, and it never touches the video file itself — it just reads a transcript and writes a small JSON edit plan that the local pipeline (ffmpeg, Whisper, Remotion, MediaPipe) executes.

## How it works

1. **Drop a video** into the Studio UI. It's transcribed locally (Whisper) and roughly cut.
2. **Tell it what you want**, in plain English, in the prompt queue panel.
3. **Claude reads the transcript + your prompt** and writes/updates an edit plan (captions, b-roll beats, camera moves, callouts, subscribe CTA, etc.) as JSON.
4. **The pipeline renders it** — ffmpeg handles cutting/stabilizing/camera movement, Remotion composites captions and graphics, audio gets scored with music + SFX.
5. **Iterate** — tweak the plan, re-render, export when it looks right. Nothing here requires leaving the app or sending the video anywhere.

## Run it

```
python3 scripts/studio.py
```

Open `http://localhost:5000`. Tabs: **Clean** (trim/stabilize/camera) → **Edit** (captions, b-roll, beats) → **Export**.

## Setup

- ffmpeg, local Whisper, Remotion, and MediaPipe installed (see each tool's own install docs)
- `PEXELS_API_KEY` — optional, only used as a stock-footage fallback when no real screenshot/asset exists ([free key](https://www.pexels.com/api/))
- Claude Code running locally to author edit plans from the prompt queue

That's it — no other API keys, no cloud render step, no external editing tool.

## Why local-only matters

Talking-head video is personal. This tool was built so editing it never means handing it to a hosted service: everything lives in a local cache folder on disk, every render happens on your CPU/GPU, and the only "AI" involved is Claude deciding *what* to place where — not a cloud pipeline processing your footage.
