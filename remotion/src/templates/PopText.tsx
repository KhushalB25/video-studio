import {
  AbsoluteFill,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * POP TEXT — character-by-character scale-pop with a lime neon glow.
 * Different from `BubblePopText` (each char in a bordered circle chip) —
 * this is bare glowing type, no chip, meant for a single large word/phrase
 * that reads as "energy" (hooks, CTAs, hype beats).
 */
export type PopTextProps = {
  text: string;
  vertical?: number;
  beat_start_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";
const MAX_CHARS = 24;

export const PopText: React.FC<PopTextProps> = ({ text, vertical, beat_start_sec }) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const localFrame = frame;

  const chars = text.slice(0, MAX_CHARS).split("");
  const cy = Math.round(height * Math.max(0.25, Math.min(0.75, vertical ?? 0.5)));
  const fontSize = Math.round(typeBase * 0.09);

  const staggerFrames = Math.round(0.06 * fps);
  const popDur = Math.round(0.4 * fps);

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div style={{
        position: "absolute", left: 0, right: 0, top: cy - fontSize / 2,
        display: "flex", flexWrap: "wrap", justifyContent: "center",
        alignItems: "center", maxWidth: width * 0.92,
        marginLeft: "auto", marginRight: "auto",
      }}>
        {chars.map((ch, i) => {
          const isSpace = ch === " ";
          const pop = spring({
            frame: localFrame - staggerFrames * i, fps, durationInFrames: popDur,
            config: { damping: 12, stiffness: 170, mass: 0.7 },
          });
          return (
            <span key={i} style={{
              fontFamily: BLOCK, fontWeight: 900, fontSize,
              color: "#FFFFFF", lineHeight: 1,
              display: "inline-block",
              whiteSpace: "pre",
              opacity: isSpace ? 1 : pop,
              transform: isSpace ? "none" : `scale(${pop})`,
              textShadow: isSpace
                ? undefined
                : "0 0 12px rgba(207,255,5,0.8), 0 0 24px rgba(207,255,5,0.4)",
            }}>
              {isSpace ? " " : ch}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
