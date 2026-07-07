import {
  AbsoluteFill,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

const LIME = "#CFFF05";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export type SoundWaveProps = {
  caption?: string;
  bar_count?: number;
  vertical?: number;
  beat_start_sec?: number;
};

/**
 * SOUND WAVE — a row of thin lime bars whose heights ripple with a
 * per-bar sine wave (different phase/frequency each), reading as a live
 * audio spectrum visualizer for the whole beat duration. No entrance/exit
 * choreography beyond a quick fade-in — the bars are meant to run
 * continuously, like "audio playing" / "podcast clip" / "listen to this"
 * beats where the visual stands in for sound.
 *
 * Hard rules:
 *  - bar_count bars (default 24), deterministic per-bar phase/frequency
 *    seeded from index so motion looks alive but is reproducible frame to
 *    frame (not Math.random)
 *  - Single lime accent: bars are the only lime element; caption stays white
 *  - vertical (0-1) positions the bar row's vertical center via pixels,
 *    never CSS percentage padding
 */
export const SoundWave: React.FC<SoundWaveProps> = ({
  caption,
  bar_count,
  vertical,
  beat_start_sec,
}) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const localSec = Math.max(0, frame / fps);

  const N = Math.max(4, Math.min(48, bar_count ?? 24));
  const rowW = Math.round(width * 0.7);
  const barGap = Math.round(rowW / N * 0.28);
  const barW = Math.round(rowW / N - barGap);
  const maxBarH = Math.round(typeBase * 0.22);
  const minBarH = Math.round(maxBarH * 0.12);

  const cy = Math.round(height * (vertical ?? 0.5));
  const rowLeft = Math.round((width - rowW) / 2);

  // Quick fade-in over the first 0.3s, no exit — bars run for the whole beat.
  const fadeInFrames = Math.round(0.3 * fps);
  const opacity = Math.min(1, Math.max(0, frame / fadeInFrames));

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div style={{
        position: "absolute",
        left: rowLeft,
        top: cy - maxBarH / 2,
        width: rowW,
        height: maxBarH,
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        opacity,
      }}>
        {Array.from({ length: N }).map((_, i) => {
          // ponytail: deterministic pseudo-random per-bar seed, not true noise
          const freq = 2 + (i % 5) * 0.3;
          const phase = i * 1.7;
          const s = Math.sin(localSec * freq + phase);
          const t = (s + 1) / 2; // 0..1
          const barH = Math.round(minBarH + (maxBarH - minBarH) * t);
          return (
            <div key={i} style={{
              width: barW,
              height: barH,
              borderRadius: Math.round(barW * 0.5),
              backgroundColor: LIME,
              boxShadow: `0 0 ${Math.round(barW * 0.6)}px rgba(207,255,5,0.35)`,
            }} />
          );
        })}
      </div>
      {caption && (
        <div style={{
          position: "absolute",
          left: 0, right: 0,
          top: cy + maxBarH / 2 + Math.round(typeBase * 0.03),
          textAlign: "center",
          fontFamily: BLOCK, fontWeight: 600, fontSize: Math.round(typeBase * 0.026),
          color: "#E9ECED",
          textShadow: "0 2px 8px rgba(0,0,0,0.7)",
          opacity,
        }}>
          {caption}
        </div>
      )}
    </AbsoluteFill>
  );
};
