import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * CIRCULAR PROGRESS — single-value ring/donut gauge with a big center
 * percentage that counts up in sync with the ring sweep. Different from
 * `RingChart`, which draws multiple sequential segments (a pie/donut
 * breakdown of several values); this is one scalar 0-100 meter, more like
 * a loading/completion indicator ("87% done", "quota used").
 *
 * Use for: progress-toward-goal beats, completion rates, single-stat gauges.
 */
export type CircularProgressProps = {
  /** 0-100 */
  value: number;
  label?: string;
  caption?: string;
  vertical?: number;
  beat_start_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export const CircularProgress: React.FC<CircularProgressProps> = ({
  value, label, caption, vertical, beat_start_sec,
}) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const localFrame = frame;
  const clamped = Math.max(0, Math.min(100, value));

  const sweepDur = Math.round(1.2 * fps);
  const sweep = interpolate(localFrame, [0, sweepDur], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const enter = spring({
    frame: localFrame, fps, durationInFrames: Math.round(0.5 * fps),
    config: { damping: 16, stiffness: 130, mass: 0.8 },
  });

  const radius = Math.round(width * 0.20);
  const strokeW = Math.round(radius * 0.16);
  const effR = radius - strokeW / 2;
  const circumference = 2 * Math.PI * effR;
  const cy = Math.round(height * Math.max(0.30, Math.min(0.72, vertical ?? 0.5)));
  const cx = Math.round(width / 2);

  const segLen = circumference * (clamped / 100) * sweep;
  const gap = circumference - segLen;

  const currentValue = Math.round(clamped * sweep);

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
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
          fill="none" stroke="rgba(255,255,255,0.10)" strokeWidth={strokeW} />
        {/* progress sweep, starts at 12 o'clock */}
        <circle cx={radius + strokeW / 2} cy={radius + strokeW / 2} r={effR}
          fill="none"
          stroke={LIME}
          strokeWidth={strokeW}
          strokeDasharray={`${segLen} ${gap}`}
          strokeLinecap="round"
          transform={`rotate(-90 ${radius + strokeW / 2} ${radius + strokeW / 2})`}
          style={{ filter: "drop-shadow(0 0 8px rgba(207,255,5,0.4))" }}
        />
      </svg>
      <div style={{
        position: "absolute", left: cx - radius, top: cy - Math.round(typeBase * 0.055),
        width: radius * 2, textAlign: "center",
        opacity: enter,
      }}>
        <div style={{
          fontFamily: BLOCK, fontWeight: 900, fontSize: Math.round(typeBase * 0.09),
          color: "#FFFFFF", lineHeight: 1, fontVariantNumeric: "tabular-nums",
        }}>
          {currentValue}%
        </div>
        {label && (
          <div style={{
            fontFamily: BLOCK, fontWeight: 600, fontSize: Math.round(typeBase * 0.024),
            color: "#B5BFC2", textTransform: "uppercase", letterSpacing: "0.06em",
            marginTop: Math.round(typeBase * 0.012),
          }}>
            {label}
          </div>
        )}
      </div>
      {caption && (
        <div style={{
          position: "absolute", left: 0, right: 0, top: cy + radius + Math.round(typeBase * 0.05),
          textAlign: "center",
          fontFamily: BLOCK, fontWeight: 700, fontSize: Math.round(typeBase * 0.026),
          color: "#E9ECED", textShadow: "0 2px 8px rgba(0,0,0,0.7)",
          opacity: interpolate(localFrame, [Math.round(0.7 * fps), Math.round(1.0 * fps)], [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
        }}>
          {caption}
        </div>
      )}
    </AbsoluteFill>
  );
};
