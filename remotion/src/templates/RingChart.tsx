import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * RING CHART — segmented circle (pie when holeRatio=0, donut when >0) with
 * sequential segment sweep-in. Ported from reactvideoeditor/remotion-templates'
 * pie-chart.tsx + donut-chart.tsx, unified into one component and rebuilt to
 * our conventions: parameterized data (not hardcoded), raisin/lime palette,
 * pixel-based vertical anchor, cardless partial overlay (speaker stays visible
 * unless segments cover most of frame).
 *
 * Use for a spoken breakdown ("60% X, 25% Y, 15% Z") where a ring reads more
 * naturally than a bar chart — market-share style splits, budget allocation,
 * status breakdowns.
 */
export type RingChartSegment = {
  label: string;
  value: number;
  /** Optional explicit hex. If omitted, cycles through the brand-adjacent
   *  palette (lime accent on first segment, silver/steel tones after). */
  color?: string;
};

export type RingChartProps = {
  segments: RingChartSegment[];
  /** 0 = solid pie. 0.55–0.65 = classic donut ring. Default 0.6. */
  holeRatio?: number;
  /** Big number in the center (donut only). If omitted, no center stat. */
  centerValue?: string;
  centerLabel?: string;
  title?: string;
  vertical?: number;
  beat_start_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const PALETTE = ["#CFFF05", "#7ad9ff", "#f472b6", "#fbbf24", "#94e0a3", "#c084fc"];
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export const RingChart: React.FC<RingChartProps> = ({
  segments, holeRatio = 0.6, centerValue, centerLabel, title, vertical, beat_start_sec,
}) => {
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const base = beat_start_sec ?? 0;

  if (!segments || segments.length === 0) return null;
  const total = segments.reduce((s, x) => s + Math.max(0, x.value), 0);
  if (total <= 0) return null;

  const exitStart = durationInFrames - 8;
  const groupOp = frame > exitStart
    ? interpolate(frame, [exitStart, durationInFrames], [1, 0],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : 1;

  const enter = spring({ frame, fps, durationInFrames: Math.round(0.5 * fps),
    config: { damping: 16, stiffness: 130, mass: 0.8 } });

  const radius = Math.round(width * 0.20);
  const strokeW = Math.round(radius * (1 - holeRatio));
  const effR = radius - strokeW / 2;
  const circumference = 2 * Math.PI * effR;
  const cy = Math.round(height * Math.max(0.30, Math.min(0.72, vertical ?? 0.52)));
  const cx = Math.round(width / 2);

  // Sequential sweep: each segment draws in over its own frame window,
  // staggered so they read as "revealing one at a time" not "all at once".
  const sweepFrames = Math.round(0.5 * fps);
  const staggerFrames = Math.round(0.18 * fps);

  let cumAngle = -90; // start at 12 o'clock
  const segData = segments.map((s, i) => {
    const pct = s.value / total;
    const segAngleDeg = pct * 360;
    const startAngle = cumAngle;
    cumAngle += segAngleDeg;
    return { ...s, pct, startAngle, segAngleDeg, i };
  });

  const legendItems = segData.map((s) => `${s.label} ${Math.round(s.pct * 100)}%`);

  return (
    <AbsoluteFill style={{ pointerEvents: "none", opacity: groupOp }}>
      {title && (
        <div style={{
          position: "absolute",
          left: 0, right: 0, top: cy - radius - Math.round(typeBase * 0.09),
          textAlign: "center",
          fontFamily: BLOCK, fontWeight: 800, fontSize: Math.round(typeBase * 0.032),
          color: "#FFFFFF", textTransform: "uppercase", letterSpacing: "0.06em",
          textShadow: "0 4px 14px rgba(0,0,0,0.85)",
          opacity: enter,
        }}>
          {title}
        </div>
      )}
      <svg
        width={radius * 2 + strokeW} height={radius * 2 + strokeW}
        style={{
          position: "absolute",
          left: cx - radius - strokeW / 2, top: cy - radius - strokeW / 2,
          transform: `scale(${interpolate(enter, [0, 1], [0.7, 1])})`,
          transformOrigin: "center",
          opacity: enter,
        }}
        viewBox={`0 0 ${radius * 2 + strokeW} ${radius * 2 + strokeW}`}
      >
        {/* track */}
        <circle cx={radius + strokeW / 2} cy={radius + strokeW / 2} r={effR}
          fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth={strokeW} />
        {segData.map((s) => {
          const segAppearF = staggerFrames * s.i;
          const segFrame = Math.max(0, frame - segAppearF);
          const drawProg = interpolate(segFrame, [0, sweepFrames], [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
          const segLen = circumference * s.pct * drawProg;
          const gap = circumference - segLen;
          const rotate = s.startAngle;
          return (
            <circle key={s.i}
              cx={radius + strokeW / 2} cy={radius + strokeW / 2} r={effR}
              fill="none"
              stroke={s.color || PALETTE[s.i % PALETTE.length]}
              strokeWidth={strokeW}
              strokeDasharray={`${segLen} ${gap}`}
              strokeLinecap="butt"
              transform={`rotate(${rotate} ${radius + strokeW / 2} ${radius + strokeW / 2})`}
              style={{
                filter: s.color === LIME || PALETTE[s.i % PALETTE.length] === LIME
                  ? "drop-shadow(0 0 8px rgba(207,255,5,0.4))" : undefined,
              }}
            />
          );
        })}
      </svg>
      {holeRatio > 0.15 && (centerValue || centerLabel) && (
        <div style={{
          position: "absolute", left: cx - radius, top: cy - Math.round(typeBase * 0.05),
          width: radius * 2, textAlign: "center",
          opacity: interpolate(frame, [Math.round(0.6 * fps), Math.round(0.9 * fps)], [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
        }}>
          {centerValue && (
            <div style={{ fontFamily: BLOCK, fontWeight: 900, fontSize: Math.round(typeBase * 0.075),
              color: "#FFFFFF", lineHeight: 1 }}>{centerValue}</div>
          )}
          {centerLabel && (
            <div style={{ fontFamily: BLOCK, fontWeight: 600, fontSize: Math.round(typeBase * 0.022),
              color: "#B5BFC2", textTransform: "uppercase", letterSpacing: "0.06em", marginTop: 4 }}>
              {centerLabel}
            </div>
          )}
        </div>
      )}
      {/* legend */}
      <div style={{
        position: "absolute", left: 0, right: 0, top: cy + radius + Math.round(typeBase * 0.05),
        display: "flex", flexWrap: "wrap", justifyContent: "center", gap: Math.round(typeBase * 0.02),
        opacity: interpolate(frame, [Math.round(0.7 * fps), Math.round(1.0 * fps)], [0, 1],
          { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
      }}>
        {segData.map((s) => (
          <div key={s.i} style={{
            display: "flex", alignItems: "center", gap: 6,
            fontFamily: BLOCK, fontWeight: 700, fontSize: Math.round(typeBase * 0.02),
            color: "#E9ECED", textShadow: "0 2px 8px rgba(0,0,0,0.7)",
          }}>
            <span style={{
              width: Math.round(typeBase * 0.014), height: Math.round(typeBase * 0.014),
              borderRadius: "50%", background: s.color || PALETTE[s.i % PALETTE.length],
              display: "inline-block",
            }} />
            {s.label} · {Math.round(s.pct * 100)}%
          </div>
        ))}
      </div>
    </AbsoluteFill>
  );
};
