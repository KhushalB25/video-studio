#!/usr/bin/env python3
"""
"Dynamic" camera movement — CapCut's 4th AI Movement Tracking mode:
"adapts to on-screen action for immersive following." Unlike zoom/shake/
soft (synthetic sine/linear motion, no awareness of content), this
actually follows the real tracked subject — reusing face_track.py's
per-frame face position data instead of a canned motion pattern.

Usage:
  dynamic_camera.py <video_path> <out.mp4> [--zoom 0.85] [--smoothing 0.85]

Pipeline:
  1. Run face_track.py's tracker (sparse — a few fps, not every frame)
  2. Interpolate + exponentially smooth the sparse face-center points into
     a full per-frame path (raw per-frame tracking is jittery; a speaker's
     actual head motion is smooth, so heavy smoothing reads as natural
     "following," not as jitter)
  3. Crop a `zoom` * original-size window centered on the smoothed face
     position for every frame (clamped so the crop window never goes
     outside the frame edges), write frames via OpenCV
  4. Remux the original audio back on top (OpenCV writes video-only)

`zoom` < 1.0 leaves room for the crop window to pan without ever showing
black edges — smaller zoom = more room to follow motion, but more of the
original frame is cropped away.
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def build_smoothed_path(face_track_pts: list[dict], total_frames: int, src_fps: float,
                         smoothing: float) -> list[tuple[float, float]]:
    """Turn sparse {t, cx, cy} samples into one (cx, cy) per output frame,
    linearly interpolating between samples and holding the last known
    position across any gap (a brief look-away / occlusion), then
    exponentially smoothing the whole path so panning reads as a deliberate
    follow instead of jittering with every small head movement."""
    if not face_track_pts:
        return [(0.5, 0.5)] * total_frames  # no face ever found — stay centered

    samples = [(p["t"], p["cx"], p["cy"]) for p in face_track_pts]
    raw = []
    si = 0
    for frame_idx in range(total_frames):
        t = frame_idx / src_fps
        while si + 1 < len(samples) and samples[si + 1][0] <= t:
            si += 1
        if si + 1 < len(samples):
            t0, x0, y0 = samples[si]
            t1, x1, y1 = samples[si + 1]
            k = 0 if t1 <= t0 else max(0.0, min(1.0, (t - t0) / (t1 - t0)))
            raw.append((x0 + (x1 - x0) * k, y0 + (y1 - y0) * k))
        else:
            raw.append((samples[si][1], samples[si][2]))

    smoothed = [raw[0]]
    for x, y in raw[1:]:
        px, py = smoothed[-1]
        smoothed.append((px + (x - px) * (1 - smoothing), py + (y - py) * (1 - smoothing)))
    return smoothed


def run(video_path: Path, out_path: Path, zoom: float = 0.85, smoothing: float = 0.85):
    import cv2
    sys.path.insert(0, str(SCRIPT_DIR))
    from face_track import track_faces

    face_pts = track_faces(video_path, sample_fps=5.0)

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    path = build_smoothed_path(face_pts, total_frames, fps, smoothing)

    crop_w = int(src_w * zoom)
    crop_h = int(src_h * zoom)

    video_only = out_path.with_suffix(".video_only.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(video_only), fourcc, fps, (crop_w, crop_h))

    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        cx, cy = path[frame_idx] if frame_idx < len(path) else path[-1]
        cx_px = int(cx * src_w)
        cy_px = int(cy * src_h)
        x0 = max(0, min(src_w - crop_w, cx_px - crop_w // 2))
        y0 = max(0, min(src_h - crop_h, cy_px - crop_h // 2))
        cropped = frame[y0:y0 + crop_h, x0:x0 + crop_w]
        writer.write(cropped)
        frame_idx += 1
        # Per-frame OpenCV read/crop/write at 4K has real per-frame Python
        # overhead (much slower than ffmpeg's native loop) — a plain silent
        # loop here was the "looks frozen for minutes" bug for this mode
        # specifically (studio.py streams this line into the UI).
        if frame_idx % 15 == 0 or frame_idx == total_frames:
            print(f"frame {frame_idx}/{total_frames}", flush=True)

    cap.release()
    writer.release()

    # Remux the original audio back on — OpenCV's VideoWriter is video-only —
    # AND scale back up to the source's original resolution: the crop above
    # shrinks frame size (crop_w/crop_h < src_w/src_h), but zoom/shake/soft
    # modes all restore the original output resolution, so leaving this one
    # at the smaller cropped size was inconsistent (and a downstream-quality
    # risk wherever original resolution is assumed).
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_only), "-i", str(video_path),
         "-map", "0:v:0", "-map", "1:a:0?", "-vf", f"scale={src_w}:{src_h}",
         "-c:v", "libx264", "-preset", "fast", "-crf", "20",
         "-c:a", "copy", "-shortest", str(out_path)],
        check=True,
    )
    video_only.unlink(missing_ok=True)


def main():
    if len(sys.argv) < 3:
        print("usage: dynamic_camera.py <video_path> <out.mp4> [--zoom 0.85] [--smoothing 0.85]", file=sys.stderr)
        sys.exit(2)
    video_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    zoom = 0.85
    smoothing = 0.85
    if "--zoom" in sys.argv:
        zoom = float(sys.argv[sys.argv.index("--zoom") + 1])
    if "--smoothing" in sys.argv:
        smoothing = float(sys.argv[sys.argv.index("--smoothing") + 1])
    run(video_path, out_path, zoom, smoothing)
    print(f"dynamic camera follow -> {out_path}")


if __name__ == "__main__":
    main()
