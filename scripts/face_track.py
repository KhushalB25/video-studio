#!/usr/bin/env python3
"""
Extract the speaker's face position across a video, frame by frame.

Foundation for face-anchored overlays: rather than a generic multi-object
tracker (ByteTrack/DeepSORT — built for surveillance-style many-object
scenes, wrong tool for a single talking-head clip), this uses MediaPipe's
lightweight BlazeFace short-range detector — purpose-built for single/few
faces, runs fast on CPU, no GPU required.

Usage:
  face_track.py <video_path> <out.json> [--sample-fps 5]

Output: <out.json> — a list of
  {"t": seconds, "cx": 0-1, "cy": 0-1, "w": 0-1, "h": 0-1}
per sampled frame (normalized face bounding-box center + size). Frames
with no detected face are omitted — a consumer should interpolate across
gaps (a brief look-away or occlusion) rather than snap the anchor to 0,0.

Sampling at a few fps (not every frame) is deliberate: face position
across a talking-head clip moves smoothly, not frame-to-frame; a beat-plan
consumer only needs enough points to interpolate a smooth follow path,
not the full 30/60fps output. Keeps this fast on longer videos.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "blaze_face_short_range.tflite"


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def track_faces(video_path: Path, sample_fps: float = 5.0) -> list[dict]:
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision

    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"face detector model missing at {MODEL_PATH} — see face_track.py header for the download URL"
        )

    base_options = mp_python.BaseOptions(model_asset_path=str(MODEL_PATH))
    options = mp_vision.FaceDetectorOptions(base_options=base_options, min_detection_confidence=0.5)
    detector = mp_vision.FaceDetector.create_from_options(options)

    cap = cv2.VideoCapture(str(video_path))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    step = max(1, round(src_fps / sample_fps))

    results = []
    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % step == 0:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            detection_result = detector.detect(mp_image)
            if detection_result.detections:
                # Largest face = the speaker (closest to camera), not a
                # background/incidental face in frame.
                best = max(
                    detection_result.detections,
                    key=lambda d: d.bounding_box.width * d.bounding_box.height,
                )
                bb = best.bounding_box
                cx = (bb.origin_x + bb.width / 2) / frame_w
                cy = (bb.origin_y + bb.height / 2) / frame_h
                results.append({
                    "t": round(frame_idx / src_fps, 3),
                    "cx": round(cx, 4),
                    "cy": round(cy, 4),
                    "w": round(bb.width / frame_w, 4),
                    "h": round(bb.height / frame_h, 4),
                })
        frame_idx += 1

    cap.release()
    detector.close()
    return results


def main():
    if len(sys.argv) < 3:
        print("usage: face_track.py <video_path> <out.json> [--sample-fps 5]", file=sys.stderr)
        sys.exit(2)
    video_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    sample_fps = 5.0
    if "--sample-fps" in sys.argv:
        sample_fps = float(sys.argv[sys.argv.index("--sample-fps") + 1])

    results = track_faces(video_path, sample_fps)
    write_json_atomic(out_path, results)
    print(f"tracked {len(results)} face positions -> {out_path}")


if __name__ == "__main__":
    main()
