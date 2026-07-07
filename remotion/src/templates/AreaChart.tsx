import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { useTypeBase } from "./motion";

/**
 * AREA CHART — gradient-filled area under a line, revealed left-to-right via
 * an animated clip-path wipe. Differs from RingChart (donut segments of a
 * whole) and MetricReveal (single hero number): this is a trend-over-points
 * shape, for "here's how it moved over time" (revenue trend, growth curve,
 * usage over weeks).
 *
 * Single lime accent: the line + its gradient fill. Axis labels/title stay
 * white/gray.
 */
export type AreaChartProps = {
  points: { label: string; value: number }[];
  title?: string;
  caption?: string;
  vertical?: number;
  beat_start_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export const AreaChart: React.FC<AreaChartProps> = ({
  points, title, caption, vertical, beat_start_sec,
}) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const f = frame;

  if (!points || points.length < 2) return null;

  const chartW = Math.round(width * 0.78);
  const chartH = Math.round(height * 0.28);
  const cx = Math.round((width - chartW) / 2);
  const cy = Math.round(height * (vertical ?? 0.45));

  const max = Math.max(...points.map((p) => p.value), 1);
  const min = Math.min(0, ...points.map((p) => p.value));
  const range = max - min || 1;
  const stepX = chartW / (points.length - 1);

  const coords = points.map((p, i) => ({
    x: Math.round(i * stepX),
    y: Math.round(chartH - ((p.value - min) / range) * chartH),
  }));

  const linePath = coords.map((c, i) => `${i === 0 ? "M" : "L"} ${c.x} ${c.y}`).join(" ");
  const areaPath = `${linePath} L ${chartW} ${chartH} L 0 ${chartH} Z`;

  const enter = spring({ frame: f, fps, durationInFrames: Math.round(0.4 * fps),
    config: { damping: 16, stiffness: 130, mass: 0.8 } });
  const wipe = interpolate(f, [0, Math.round(1.0 * fps)], [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const revealW = Math.round(chartW * wipe);

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      {title && (
        <div style={{
          position: "absolute",
          left: cx, top: cy - Math.round(typeBase * 0.09),
          fontFamily: BLOCK, fontWeight: 800, fontSize: Math.round(typeBase * 0.032),
          color: "#FFFFFF", textTransform: "uppercase", letterSpacing: "0.06em",
          textShadow: "0 4px 14px rgba(0,0,0,0.85)",
          opacity: enter,
        }}>
          {title}
        </div>
      )}
      <svg
        width={chartW} height={chartH}
        style={{ position: "absolute", left: cx, top: cy, opacity: enter }}
        viewBox={`0 0 ${chartW} ${chartH}`}
      >
        <defs>
          <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={LIME} stopOpacity="0.45" />
            <stop offset="100%" stopColor={LIME} stopOpacity="0" />
          </linearGradient>
          <clipPath id="revealClip">
            <rect x={0} y={0} width={revealW} height={chartH} />
          </clipPath>
        </defs>
        <g clipPath="url(#revealClip)">
          <path d={areaPath} fill="url(#areaGrad)" />
          <path d={linePath} fill="none" stroke={LIME} strokeWidth={Math.round(chartH * 0.02)}
            strokeLinecap="round" strokeLinejoin="round"
            style={{ filter: "drop-shadow(0 0 8px rgba(207,255,5,0.4))" }} />
        </g>
      </svg>
      <div style={{
        position: "absolute", left: cx, top: cy + chartH + Math.round(typeBase * 0.014),
        width: chartW, display: "flex", justifyContent: "space-between",
        opacity: interpolate(f, [Math.round(0.6 * fps), Math.round(0.9 * fps)], [0, 1],
          { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
      }}>
        {points.map((p, i) => (
          <div key={i} style={{
            fontFamily: BLOCK, fontWeight: 600, fontSize: Math.round(typeBase * 0.018),
            color: "#B5BFC2", textAlign: "center",
          }}>
            {p.label}
          </div>
        ))}
      </div>
      {caption && (
        <div style={{
          position: "absolute", left: cx, top: cy + chartH + Math.round(typeBase * 0.06),
          width: chartW, textAlign: "center",
          fontFamily: BLOCK, fontWeight: 600, fontSize: Math.round(typeBase * 0.024),
          color: "#E9ECED", textShadow: "0 2px 8px rgba(0,0,0,0.7)",
          opacity: interpolate(f, [Math.round(0.9 * fps), Math.round(1.2 * fps)], [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
        }}>
          {caption}
        </div>
      )}
    </AbsoluteFill>
  );
};
