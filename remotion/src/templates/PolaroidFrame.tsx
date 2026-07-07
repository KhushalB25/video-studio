import {
  AbsoluteFill,
  Img,
  staticFile,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * POLAROID FRAME — a single polaroid-style photo that drops in from above
 * with a spring, settling into a slight rotation. Use for one standout photo
 * moment with a handwritten-feel caption below it. Differs from photo_stack
 * (3 overlapping photos) and image_card (glassy card, no physical-object
 * feel) — this is a single physical polaroid dropping into frame.
 */
export type PolaroidFrameProps = {
  image: string;
  caption?: string;
};

const RAISIN = "#0F121A";

const resolveSrc = (s: string): string => /^https?:\/\//i.test(s) ? s : staticFile(s);

export const PolaroidFrame: React.FC<PolaroidFrameProps> = ({ image, caption }) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const dropIn = spring({
    frame,
    fps,
    config: { damping: 14, stiffness: 110, mass: 0.9 },
  });
  const translateY = interpolate(dropIn, [0, 1], [-height * 0.4, 0]);
  const rotation = interpolate(dropIn, [0, 1], [8, -3]);
  const opacity = interpolate(dropIn, [0, 0.3], [0, 1], { extrapolateRight: "clamp" });

  const cardW = width * 0.5;
  const cardH = cardW * 1.15;
  const pad = cardW * 0.05;

  return (
    <AbsoluteFill style={{ backgroundColor: RAISIN, alignItems: "center", justifyContent: "center" }}>
      <div style={{
        width: cardW,
        backgroundColor: "white",
        padding: `${pad}px ${pad}px ${pad * 3.5}px ${pad}px`,
        borderRadius: width * 0.005,
        transform: `translateY(${translateY}px) rotate(${rotation}deg)`,
        opacity,
        boxShadow: "0 24px 60px rgba(0,0,0,0.55)",
      }}>
        <div style={{ width: "100%", height: cardH, borderRadius: width * 0.003, overflow: "hidden", backgroundColor: RAISIN }}>
          <Img src={resolveSrc(image)} style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
        </div>
        {caption && (
          <p style={{
            textAlign: "center", color: "#374151",
            fontSize: typeBase * 0.03, fontWeight: 500,
            margin: 0, marginTop: pad,
            fontFamily: "Georgia, serif", fontStyle: "italic",
          }}>
            {caption}
          </p>
        )}
      </div>
    </AbsoluteFill>
  );
};
