import {
  AbsoluteFill,
  Img,
  staticFile,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * IMAGE ZOOM REVEAL — a single image scales down from 2x and unblurs over the
 * first ~0.7s, then an optional caption fades in over it. Use for a dramatic
 * single-image reveal ("here's what it looks like") — a slower, more
 * cinematic entrance than image_card's simple fade+rise.
 */
export type ImageZoomRevealProps = {
  image: string;
  caption?: string;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

const resolveSrc = (s: string): string => /^https?:\/\//i.test(s) ? s : staticFile(s);

export const ImageZoomReveal: React.FC<ImageZoomRevealProps> = ({ image, caption }) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const revealFrames = Math.round(0.7 * fps);
  const scale = interpolate(frame, [0, revealFrames], [2, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const blur = interpolate(frame, [0, revealFrames], [16, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const captionOpacity = interpolate(frame, [revealFrames * 0.7, revealFrames * 1.3], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });

  const marginX = width * 0.08;
  const marginY = height * 0.14;

  return (
    <AbsoluteFill style={{ backgroundColor: RAISIN, alignItems: "center", justifyContent: "center" }}>
      <div style={{
        position: "absolute",
        left: marginX, right: marginX, top: marginY, bottom: marginY,
        borderRadius: width * 0.03,
        overflow: "hidden",
        border: `2px solid rgba(207,255,5,0.4)`,
        boxShadow: "0 20px 60px rgba(0,0,0,0.6)",
      }}>
        <Img src={resolveSrc(image)} style={{
          width: "100%", height: "100%", objectFit: "cover",
          transform: `scale(${scale})`, filter: `blur(${blur}px)`,
        }} />
        {caption && (
          <div style={{
            position: "absolute", left: 0, right: 0, bottom: 0,
            padding: `${Math.round(typeBase * 0.03)}px ${Math.round(typeBase * 0.03)}px`,
            background: "linear-gradient(transparent, rgba(0,0,0,0.75))",
            opacity: captionOpacity,
            textAlign: "center", fontFamily: BLOCK, fontWeight: 700,
            fontSize: typeBase * 0.034, color: LIME,
          }}>
            {caption}
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};
