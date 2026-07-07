import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * BOUNCE TITLE — title + subtitle spring bounce entrance. Unlike the smooth
 * fade/rise used by most templates here, this deliberately overshoots for a
 * punchy, playful landing. Use for hook openers, reveals, and any beat that
 * wants energy rather than restraint.
 */
export type BounceTitleProps = {
  title: string;
  subtitle?: string;
  beat_start_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export const BounceTitle: React.FC<BounceTitleProps> = ({
  title, subtitle, beat_start_sec,
}) => {
  const { fps, width } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const localFrame = frame;

  // Low damping (vs the usual 14-18) + high stiffness = a visible overshoot
  // bounce rather than a critically-damped settle. This is the whole point
  // of this template, so it breaks from the house spring config on purpose.
  const titleSpring = spring({
    frame: localFrame, fps, durationInFrames: Math.round(0.7 * fps),
    config: { damping: 9, stiffness: 180, mass: 0.7 },
  });

  const subtitleFrame = Math.max(0, localFrame - Math.round(0.18 * fps));
  const subtitleEnter = spring({
    frame: subtitleFrame, fps, durationInFrames: Math.round(0.5 * fps),
    config: { damping: 16, stiffness: 130, mass: 0.7 },
  });

  return (
    <AbsoluteFill>
      <AbsoluteFill style={{ backgroundColor: RAISIN }} />
      <AbsoluteFill style={{
        display: "flex", flexDirection: "column", alignItems: "center",
        justifyContent: "center", textAlign: "center", padding: width * 0.06,
      }}>
        <div style={{
          fontFamily: BLOCK, fontWeight: 900, fontSize: Math.round(typeBase * 0.075),
          color: "#FFFFFF", lineHeight: 1.05, letterSpacing: "-0.01em",
          opacity: interpolate(titleSpring, [0, 1], [0, 1]),
          transform: `scale(${interpolate(titleSpring, [0, 1], [0.4, 1])})`,
        }}>
          {title}
        </div>
        {subtitle && (
          <div style={{
            fontFamily: BLOCK, fontWeight: 600, fontSize: Math.round(typeBase * 0.032),
            color: LIME, marginTop: Math.round(typeBase * 0.028),
            opacity: subtitleEnter,
            transform: `translateY(${interpolate(subtitleEnter, [0, 1], [16, 0])}px)`,
          }}>
            {subtitle}
          </div>
        )}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
