import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { useTypeBase } from "./motion";

/**
 * PROGRESS BARS — horizontal skill/metric bars that fill to their target
 * width, staggered top to bottom (same stagger idea as RingChart's segment
 * sweep, applied to bars instead of arcs). Differs from ComparisonBars
 * (before/after pairs per row) and RingChart (whole-circle proportions):
 * this is a plain ranked list of independent metrics ("React 90%, Vue 60%").
 *
 * Single lime accent: the fill. Track stays raisin, labels/numbers stay
 * white/gray.
 */
export type ProgressBarsProps = {
  bars: { label: string; value: number; max?: number }[];
  title?: string;
  beat_start_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";
const MAX_BARS = 6;

export const ProgressBars: React.FC<ProgressBarsProps> = ({ bars, title, beat_start_sec }) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const f = frame;

  if (!bars || bars.length === 0) return null;
  const rows = bars.slice(0, MAX_BARS);

  const titleEnter = spring({ frame: f, fps, durationInFrames: Math.round(0.4 * fps),
    config: { damping: 16, stiffness: 130, mass: 0.8 } });

  const trackW = Math.round(width * 0.7);
  const trackX = Math.round((width - trackW) / 2);
  const rowH = Math.round(typeBase * 0.09);
  const barH = Math.round(typeBase * 0.032);
  const startY = Math.round(height * 0.32);
  const staggerFrames = Math.round(0.15 * fps);
  const fillFrames = Math.round(0.5 * fps);

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      {title && (
        <div style={{
          position: "absolute", left: trackX, top: Math.round(startY - typeBase * 0.10),
          fontFamily: BLOCK, fontWeight: 800, fontSize: Math.round(typeBase * 0.032),
          color: "#FFFFFF", textTransform: "uppercase", letterSpacing: "0.06em",
          textShadow: "0 4px 14px rgba(0,0,0,0.85)",
          opacity: titleEnter,
        }}>
          {title}
        </div>
      )}
      {rows.map((b, i) => {
        const max = b.max ?? Math.max(...rows.map((r) => r.max ?? r.value), b.value, 1);
        const pct = Math.max(0, Math.min(1, b.value / max));
        const appearF = staggerFrames * i;
        const barSpring = spring({
          frame: f - appearF, fps, durationInFrames: fillFrames,
          config: { damping: 16, stiffness: 130, mass: 0.75 },
        });
        const rowEnter = interpolate(barSpring, [0, 1], [0, 1]);
        const y = startY + i * rowH;

        return (
          <div key={i} style={{
            position: "absolute", left: trackX, top: y, width: trackW,
            opacity: rowEnter,
          }}>
            <div style={{
              display: "flex", justifyContent: "space-between", marginBottom: Math.round(typeBase * 0.01),
              fontFamily: BLOCK, fontWeight: 700, fontSize: Math.round(typeBase * 0.024),
              color: "#E9ECED",
            }}>
              <span>{b.label}</span>
              <span style={{ color: LIME }}>{Math.round(pct * 100)}%</span>
            </div>
            <div style={{
              width: trackW, height: barH, borderRadius: Math.round(barH / 2),
              backgroundColor: RAISIN, overflow: "hidden",
            }}>
              <div style={{
                width: Math.round(trackW * pct * barSpring), height: barH,
                borderRadius: Math.round(barH / 2),
                backgroundColor: LIME,
                boxShadow: "0 0 8px rgba(207,255,5,0.4)",
              }} />
            </div>
          </div>
        );
      })}
    </AbsoluteFill>
  );
};
