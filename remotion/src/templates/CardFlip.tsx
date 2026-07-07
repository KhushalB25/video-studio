import {
  AbsoluteFill,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
  Easing,
} from "remotion";
import { useTypeBase } from "./motion";

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export type CardFlipProps = {
  front_text: string;
  back_text: string;
  front_label?: string;
  back_label?: string;
  /** Seconds after beat_start_sec when the flip happens. Default 1.5. */
  flip_sec?: number;
  beat_start_sec?: number;
};

/**
 * CARD FLIP — a single card that physically rotates 180deg around its
 * vertical axis mid-beat, revealing back_text after showing front_text.
 * Different from a plain entrance/exit template: the flip is a TRANSITION
 * that happens partway through the beat (default 1.5s in), not just an
 * intro animation. Use for "here's the claim... but here's the truth" /
 * "before → after" / definition-then-answer beats.
 *
 * Choreography (relative to beat_start_sec):
 *   0.00s        card sits still showing front face (white text on raisin)
 *   flip_sec     card rotates 0deg → 180deg over ~0.6s (ease-in-out)
 *   flip_sec+    back face settles, showing back_text (raisin text on lime —
 *                the flip also flips the palette, single lime accent stays
 *                intact because only one face is ever visible at a time)
 *
 * Hard rules:
 *  - CSS 3D transform only (perspective on wrapper, preserve-3d, backface
 *    hidden) — no spring bounce on the flip itself, it should read as a
 *    deliberate mechanical turn
 *  - front/back label chip (optional) sits above the main text, small caps
 */
export const CardFlip: React.FC<CardFlipProps> = ({
  front_text,
  back_text,
  front_label,
  back_label,
  flip_sec,
  beat_start_sec,
}) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const flipAt = flip_sec ?? 1.5;

  const cardW = Math.round(width * (width >= height ? 0.44 : 0.78));
  const cardH = Math.round(cardW * 0.62);
  const cx = Math.round(width / 2);
  const cy = Math.round(height * 0.5);

  const flipStartFrame = Math.round(flipAt * fps);
  const flipDur = Math.round(0.6 * fps);
  const rotation = interpolate(
    frame,
    [flipStartFrame, flipStartFrame + flipDur],
    [0, 180],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.bezier(0.45, 0, 0.2, 1) },
  );
  const showingBack = rotation > 90;

  // Gentle settle-in for the whole card on first appearance.
  const entrance = interpolate(frame, [0, Math.round(0.35 * fps)], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.out(Easing.cubic),
  });

  const labelSize = Math.round(typeBase * 0.024);
  const textSize = Math.round(typeBase * 0.042);

  const faceBase: React.CSSProperties = {
    position: "absolute",
    inset: 0,
    backfaceVisibility: "hidden",
    borderRadius: Math.round(typeBase * 0.024),
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    textAlign: "center",
    padding: Math.round(typeBase * 0.04),
    boxShadow: `0 ${Math.round(typeBase * 0.02)}px ${Math.round(typeBase * 0.05)}px rgba(0,0,0,0.35)`,
  };

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div style={{
        position: "absolute",
        left: cx - cardW / 2,
        top: cy - cardH / 2,
        width: cardW,
        height: cardH,
        perspective: 1600,
        opacity: entrance,
        transform: `scale(${interpolate(entrance, [0, 1], [0.92, 1])})`,
      }}>
        <div style={{
          position: "relative",
          width: "100%",
          height: "100%",
          transformStyle: "preserve-3d",
          transform: `rotateY(${rotation}deg)`,
        }}>
          {/* Front face — raisin card, white text */}
          <div style={{ ...faceBase, backgroundColor: RAISIN, border: `2px solid rgba(255,255,255,0.08)` }}>
            {front_label && (
              <div style={{
                fontFamily: BLOCK, fontWeight: 700, fontSize: labelSize,
                color: LIME, textTransform: "uppercase", letterSpacing: "0.1em",
                marginBottom: Math.round(typeBase * 0.018),
              }}>
                {front_label}
              </div>
            )}
            <div style={{
              fontFamily: BLOCK, fontWeight: 800, fontSize: textSize,
              color: "#FFFFFF", lineHeight: 1.2,
            }}>
              {front_text}
            </div>
          </div>
          {/* Back face — lime card, raisin text (rotated 180deg baked in) */}
          <div style={{
            ...faceBase,
            backgroundColor: LIME,
            transform: "rotateY(180deg)",
          }}>
            {back_label && (
              <div style={{
                fontFamily: BLOCK, fontWeight: 700, fontSize: labelSize,
                color: RAISIN, textTransform: "uppercase", letterSpacing: "0.1em",
                marginBottom: Math.round(typeBase * 0.018), opacity: 0.7,
              }}>
                {back_label}
              </div>
            )}
            <div style={{
              fontFamily: BLOCK, fontWeight: 800, fontSize: textSize,
              color: RAISIN, lineHeight: 1.2,
            }}>
              {back_text}
            </div>
          </div>
        </div>
      </div>
    </AbsoluteFill>
  );
};
