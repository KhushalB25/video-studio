import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * BUBBLE POP TEXT — characters pop into circular bordered "bubble" chips
 * one at a time, staggered. Different from `BounceTitle` (whole-block
 * bounce) and `PopText` (glowing bare characters, no chip) — this one gives
 * each character its own bordered circle, good for short punchy words/tags
 * ("FAST", "FREE", "NEW") rather than long sentences.
 */
export type BubblePopTextProps = {
  text: string;
  vertical?: number;
  beat_start_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";
const MAX_CHARS = 24;

export const BubblePopText: React.FC<BubblePopTextProps> = ({
  text, vertical, beat_start_sec,
}) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const localFrame = frame;

  const chars = text.slice(0, MAX_CHARS).split("");
  const chipSize = Math.round(typeBase * 0.10);
  const gap = Math.round(typeBase * 0.02);
  const cy = Math.round(height * Math.max(0.25, Math.min(0.75, vertical ?? 0.5)));

  const staggerFrames = Math.round(0.065 * fps);
  const popDur = Math.round(0.4 * fps);

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div style={{
        position: "absolute", left: 0, right: 0, top: cy - chipSize / 2,
        display: "flex", flexWrap: "wrap", justifyContent: "center",
        alignItems: "center", gap, maxWidth: width * 0.9,
        marginLeft: "auto", marginRight: "auto",
      }}>
        {chars.map((ch, i) => {
          const isSpace = ch === " ";
          const pop = spring({
            frame: localFrame - staggerFrames * i, fps, durationInFrames: popDur,
            config: { damping: 12, stiffness: 170, mass: 0.7 },
          });
          if (isSpace) {
            return <div key={i} style={{ width: chipSize * 0.5, height: chipSize }} />;
          }
          return (
            <div key={i} style={{
              width: chipSize, height: chipSize, borderRadius: "50%",
              border: `${Math.round(chipSize * 0.06)}px solid ${LIME}`,
              backgroundColor: RAISIN,
              display: "flex", alignItems: "center", justifyContent: "center",
              opacity: interpolate(pop, [0, 1], [0, 1]),
              transform: `scale(${interpolate(pop, [0, 1], [0, 1])})`,
            }}>
              <span style={{
                fontFamily: BLOCK, fontWeight: 800, fontSize: Math.round(chipSize * 0.46),
                color: "#FFFFFF", textTransform: "uppercase", lineHeight: 1,
              }}>
                {ch}
              </span>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
