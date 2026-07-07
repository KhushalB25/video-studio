import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { useTypeBase } from "./motion";

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export type PulseTextProps = {
  text: string;
  vertical?: number;
  beat_start_sec?: number;
};

/**
 * PULSE TEXT — continuously breathing text with a soft lime glow halo.
 * Unlike MetricReveal/BulletedList (one-shot spring entrances), this is a
 * LOOPING ambient pulse: scale oscillates ~1.0↔1.04 on a sine wave for the
 * whole beat, driven directly by frame/fps rather than spring. Use for a
 * single emphasized word/phrase that should feel "alive" while spoken —
 * a hook line, a warning word, a call-to-action — not for entrance-heavy
 * reveals.
 *
 * Single lime accent: the glow halo is lime; the text itself is white and
 * only tints toward lime at the peak of each pulse.
 */
export const PulseText: React.FC<PulseTextProps> = ({ text, vertical, beat_start_sec }) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const t = Math.max(0, frame / fps);
  // Continuous breathing loop: ~0.9Hz, eased in over the first 0.3s so it
  // doesn't jump-cut into the pulse on frame 0.
  const settle = interpolate(t, [0, 0.3], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const wave = (Math.sin(t * Math.PI * 1.8) + 1) / 2; // 0..1
  const scale = 1 + 0.04 * wave * settle;
  const glowStrength = 0.35 + 0.45 * wave * settle;

  const fontSize = Math.round(typeBase * 0.095);
  const cy = Math.round(height * (vertical ?? 0.5));

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div style={{
        position: "absolute",
        left: 0, right: 0, top: cy - fontSize,
        display: "flex", justifyContent: "center", alignItems: "center",
      }}>
        {/* blurred halo duplicate, lime, pulsing opacity/blur in sync */}
        <div style={{
          position: "absolute",
          fontFamily: BLOCK, fontWeight: 800, fontSize,
          color: LIME,
          filter: `blur(${Math.round(fontSize * 0.18)}px)`,
          opacity: glowStrength * 0.6,
          transform: `scale(${scale})`,
          textAlign: "center",
          whiteSpace: "pre-wrap",
        }}>
          {text}
        </div>
        <div style={{
          position: "relative",
          fontFamily: BLOCK, fontWeight: 800, fontSize,
          color: "#FFFFFF",
          transform: `scale(${scale})`,
          textAlign: "center",
          whiteSpace: "pre-wrap",
          textShadow: `0 0 ${Math.round(fontSize * 0.25 * glowStrength)}px rgba(207,255,5,${0.5 * glowStrength})`,
        }}>
          {text}
        </div>
      </div>
    </AbsoluteFill>
  );
};
