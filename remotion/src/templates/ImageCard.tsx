import {
  AbsoluteFill,
  Img,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * IMAGE CARD — a b-roll image shown in a glassy card in the bottom ~36% of
 * the frame, off the speaker's chin. Image preserves aspect (contain).
 */
export type ImageCardProps = {
  src: string;
  caption?: string;
  /** "wide" (default) = full-width card. "hug" = card auto-shrinks to image's natural aspect (good for portrait images). */
  card_fit?: "wide" | "hug";
  /** Card top edge (fraction of frame height, 0=top 1=bottom). Default 0.60. */
  card_top?: number;
  /** Card gap from bottom edge (fraction of frame height). Default 0.04. */
  card_bottom?: number;
  /** Card horizontal margin (fraction of frame width). Default 0.06. */
  card_margin?: number;
  /** Image scale inside card (1.0 = fill, lower = shrink). Default 1.0. */
  image_scale?: number;
};

const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export const ImageCard: React.FC<ImageCardProps> = ({ src, caption, card_fit, card_top, card_bottom, card_margin, image_scale }) => {
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const enter = spring({
    frame, fps, durationInFrames: Math.round(0.5 * fps),
    config: { damping: 18, stiffness: 120, mass: 0.8 },
  });
  const exitStart = durationInFrames - 8;
  const exitP = frame > exitStart
    ? interpolate(frame, [exitStart, durationInFrames], [0, 1],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : 0;
  const opacity = enter * (1 - exitP);
  const ty = interpolate(enter, [0, 1], [height * 0.06, 0])
    + interpolate(exitP, [0, 1], [0, height * 0.04]);

  const kb = 1 + 0.04 * interpolate(
    frame, [0, durationInFrames], [0, 1],
    { extrapolateRight: "clamp" },
  );

  const margin = width * (card_margin ?? 0.06);
  const cardTop = height * (card_top ?? 0.60);
  const cardBottom = height * (card_bottom ?? 0.04);
  const imgScale = image_scale ?? 1.0;
  const radius = width * 0.05;
  const pad = width * 0.028;
  const borderW = Math.max(2, Math.round(typeBase * 0.005));
  const capH = caption ? typeBase * 0.085 : 0;

  const isHug = card_fit === "hug";
  const cardH = height - cardTop - cardBottom;
  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div style={{
        position: "absolute",
        left: 0, right: 0,
        top: cardTop,
        height: cardH,
        opacity,
        transform: `translateY(${ty}px)`,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}>
        <div style={{
          position: "relative",
          width: isHug ? "fit-content" : `calc(100% - ${margin * 2}px)`,
          maxWidth: width - margin * 2,
          height: "100%",
          borderRadius: radius,
          backgroundColor: "rgba(15,18,26,0.62)",
          backdropFilter: "blur(26px)",
          WebkitBackdropFilter: "blur(26px)",
          border: `${borderW}px solid rgba(207,255,5,0.55)`,
          boxShadow: [
            `0 0 ${typeBase * 0.06}px rgba(207,255,5,0.30)`,
            `0 ${typeBase * 0.03}px ${typeBase * 0.07}px rgba(0,0,0,0.55)`,
          ].join(", "),
          padding: pad,
          boxSizing: "border-box",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          overflow: "hidden",
        }}>
          <div style={{
            height: cardH - pad * 2 - capH,
            width: isHug ? "auto" : "100%",
            borderRadius: radius * 0.62,
            overflow: "hidden",
            backgroundColor: RAISIN,
            display: isHug ? "inline-block" : "block",
          }}>
            <Img
              src={src}
              style={{
                height: "100%",
                width: isHug ? "auto" : "100%",
                objectFit: isHug ? undefined : "contain",
                display: "block",
                transform: `scale(${kb * imgScale})`,
                transformOrigin: "center",
              }}
            />
          </div>

        {caption && (
          <div style={{
            position: "absolute",
            left: pad, right: pad, bottom: pad * 0.4,
            height: capH,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontFamily: BLOCK,
            fontWeight: 700,
            fontSize: typeBase * 0.034,
            color: "#FFFFFF",
            textAlign: "center",
            letterSpacing: "0.01em",
          }}>
            {caption}
          </div>
        )}
        </div>
      </div>
    </AbsoluteFill>
  );
};
