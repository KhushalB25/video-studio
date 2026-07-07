import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * COUNTDOWN REVEAL — big numbers ticking down to a final word (default "GO"),
 * one per beat of time. Ported/rebuilt from remotion-templates' countdown-timer
 * + countdown-intro, unified into one parameterized component.
 *
 * Use for launch-hype moments, "in 3... 2... 1..." beats, or timed reveals
 * ("wait for it") — full-frame takeover, short duration (2-4s typical).
 */
export type CountdownRevealProps = {
  /** Sequence of labels shown one after another, evenly split across the
   *  beat duration. Default ["3","2","1","GO"]. */
  steps?: string[];
  subtitle?: string;
  beat_start_sec?: number;
  beat_end_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export const CountdownReveal: React.FC<CountdownRevealProps> = ({
  steps, subtitle, beat_start_sec, beat_end_sec,
}) => {
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const list = steps && steps.length ? steps : ["3", "2", "1", "GO"];

  const totalDur = beat_end_sec != null && beat_start_sec != null
    ? beat_end_sec - beat_start_sec : durationInFrames / fps;
  const framesPerStep = Math.max(4, Math.floor((totalDur * fps) / list.length));
  const currentIndex = Math.min(Math.floor(frame / framesPerStep), list.length - 1);
  const frameInStep = frame - currentIndex * framesPerStep;
  const isFinal = currentIndex === list.length - 1;

  const pop = spring({
    frame: frameInStep, fps,
    durationInFrames: Math.round(0.35 * fps),
    config: { damping: 12, stiffness: isFinal ? 260 : 200, mass: 0.5 },
  });
  const scale = interpolate(pop, [0, 1], [0.4, 1], { extrapolateRight: "clamp" });
  const fadeOut = interpolate(frameInStep, [framesPerStep - 6, framesPerStep], [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const opacity = Math.min(pop, fadeOut);

  const exitStart = durationInFrames - 6;
  const groupOp = frame > exitStart
    ? interpolate(frame, [exitStart, durationInFrames], [1, 0],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : 1;

  return (
    <AbsoluteFill style={{
      pointerEvents: "none", opacity: groupOp,
      display: "flex", alignItems: "center", justifyContent: "center",
      flexDirection: "column",
    }}>
      <div style={{
        fontFamily: BLOCK, fontWeight: 900,
        fontSize: isFinal ? Math.round(typeBase * 0.20) : Math.round(typeBase * 0.28),
        color: isFinal ? LIME : "#FFFFFF",
        transform: `scale(${scale})`,
        opacity,
        textShadow: isFinal
          ? "0 0 40px rgba(207,255,5,0.6), 0 10px 30px rgba(0,0,0,0.9)"
          : "0 10px 30px rgba(0,0,0,0.9)",
        letterSpacing: isFinal ? "0.04em" : "0",
      }}>
        {list[currentIndex]}
      </div>
      {subtitle && (
        <div style={{
          marginTop: Math.round(typeBase * 0.03),
          fontFamily: BLOCK, fontWeight: 700, fontSize: Math.round(typeBase * 0.028),
          color: "#B5BFC2", textTransform: "uppercase", letterSpacing: "0.08em",
          opacity: interpolate(frame, [4, 14], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
        }}>
          {subtitle}
        </div>
      )}
    </AbsoluteFill>
  );
};
