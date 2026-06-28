# video-edit skill

A Claude Code skill for editing YouTube videos. Built by Luuk Alleman.

## What's inside

- `SKILL.md` — the prompt Claude uses to orchestrate edits
- `knowledge/` — recipes, style guides, editing rules, music library reference
- `scripts/` — Python + bash helpers Claude calls (transcript polish, b-roll fetch, render, etc.)
- `assets/` — brand assets used in renders (logos, subscribe bug)

## Setup

1. **Drop the folder into `~/.claude/skills/`** so Claude Code picks it up:
   ```
   mv video-edit ~/.claude/skills/
   ```

2. **Environment variables** the scripts expect (set in your shell or `.env`):
   - `PEXELS_API_KEY` — free at https://www.pexels.com/api/
   - `ANTHROPIC_API_KEY` — for `polish_transcript.py`
   - `OPENAI_API_KEY` — used by some scripts

3. **NOT included in this download:**
   - `remotion/` — the Remotion render engine (491MB, install separately: https://www.remotion.dev/)
   - `music/`, `sfx/` — copyrighted; bring your own library
   - `config/youtube_upload_token.json` — generate your own via Google Cloud Console + the YouTube Data API OAuth flow

4. **Quick test:**
   In Claude Code: invoke the skill with a video path and see what happens.

## Questions?

Reach out at luuk@alleman.nl or via the chat at build-loop.ai.
