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
 * IMAGE CAROUSEL — horizontal row of images, center one scaled up & full
 * opacity, neighbors shrunk/faded to the sides. Advances one slot every
 * `slot_sec` seconds, cycling through the array. Use for "here's a series of
 * things, one after another" (product shots, step screenshots) where a
 * carousel feel (things sliding past) reads better than a static grid.
 */
export type ImageCarouselProps = {
  images: string[];
  labels?: string[];
  slot_sec?: number;
};

const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

const resolveSrc = (s: string): string => /^https?:\/\//i.test(s) ? s : staticFile(s);

export const ImageCarousel: React.FC<ImageCarouselProps> = ({ images, labels, slot_sec }) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const slides = images || [];
  const slotFrames = Math.round((slot_sec ?? 1.4) * fps);
  const progress = slotFrames > 0 ? frame / slotFrames : 0;

  const cardW = width * 0.5;
  const cardH = height * 0.34;
  const spacing = cardW * 0.62;

  return (
    <AbsoluteFill style={{ backgroundColor: RAISIN, alignItems: "center", justifyContent: "center" }}>
      <div style={{ position: "relative", width: "100%", height: cardH * 1.3 }}>
        {slides.map((src, i) => {
          const offset = i - progress;
          const tx = offset * spacing;
          const scale = interpolate(Math.abs(offset), [0, 1, 2], [1, 0.72, 0.5], { extrapolateRight: "clamp" });
          const opacity = interpolate(Math.abs(offset), [0, 1, 2], [1, 0.55, 0.15], { extrapolateRight: "clamp" });
          return (
            <div key={i} style={{
              position: "absolute", left: "50%", top: "50%",
              width: cardW, height: cardH,
              transform: `translate(-50%,-50%) translateX(${tx}px) scale(${scale})`,
              opacity,
              borderRadius: width * 0.025,
              overflow: "hidden",
              boxShadow: "0 20px 50px rgba(0,0,0,0.55)",
              backgroundColor: RAISIN,
              display: "flex", flexDirection: "column",
            }}>
              <Img src={resolveSrc(src)} style={{ width: "100%", height: "100%", objectFit: "cover", flex: 1 }} />
              {labels?.[i] && (
                <div style={{
                  position: "absolute", left: 0, right: 0, bottom: 0,
                  padding: `${Math.round(typeBase * 0.018)}px 0`,
                  textAlign: "center", fontFamily: BLOCK, fontWeight: 700,
                  fontSize: typeBase * 0.026, color: "#FFFFFF",
                  background: "linear-gradient(transparent, rgba(0,0,0,0.7))",
                }}>
                  {labels[i]}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
