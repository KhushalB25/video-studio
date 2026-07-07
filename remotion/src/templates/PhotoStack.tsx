import {
  AbsoluteFill,
  Img,
  staticFile,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * PHOTO STACK — 3 overlapping polaroid-style photos with alternating
 * rotation offsets, staggered spring entrance (each lands ~8 frames after
 * the previous). Use for "a handful of moments/photos" in a casual, physical
 * stack — reads more personal than a grid. Differs from polaroid_frame
 * (single photo, drop-in from above) and gallery_grid (evenly tiled, no
 * overlap/rotation).
 */
export type PhotoStackProps = {
  images: string[];
  captions?: string[];
};

const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";
const ROTATIONS = [-6, 0, 6];

const resolveSrc = (s: string): string => /^https?:\/\//i.test(s) ? s : staticFile(s);

export const PhotoStack: React.FC<PhotoStackProps> = ({ images, captions }) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const photos = (images || []).slice(0, 3);
  const cardW = width * 0.42;
  const cardH = cardW * 1.25;

  return (
    <AbsoluteFill style={{ backgroundColor: RAISIN, alignItems: "center", justifyContent: "center" }}>
      {photos.map((src, i) => {
        const delay = i * 8;
        const appear = spring({
          frame: Math.max(frame - delay, 0),
          fps,
          config: { damping: 14, stiffness: 130, mass: 0.8 },
        });
        const rotation = ROTATIONS[i % ROTATIONS.length];
        const offsetX = (i - 1) * cardW * 0.28;
        const offsetY = (i - 1) * -cardH * 0.06;
        return (
          <div key={i} style={{
            position: "absolute",
            width: cardW, height: cardH,
            transform: `translate(${offsetX}px, ${offsetY}px) rotate(${rotation}deg) scale(${appear})`,
            opacity: appear,
            borderRadius: width * 0.01,
            border: `${Math.round(width * 0.012)}px solid white`,
            boxShadow: "0 18px 45px rgba(0,0,0,0.5)",
            backgroundColor: "white",
            display: "flex", flexDirection: "column",
            zIndex: i,
          }}>
            <div style={{ flex: 1, overflow: "hidden", backgroundColor: RAISIN }}>
              <Img src={resolveSrc(src)} style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
            </div>
            {captions?.[i] && (
              <div style={{
                padding: `${Math.round(cardH * 0.05)}px 0`,
                textAlign: "center", fontFamily: BLOCK, fontStyle: "italic",
                fontSize: typeBase * 0.026, color: "#374151",
              }}>
                {captions[i]}
              </div>
            )}
          </div>
        );
      })}
    </AbsoluteFill>
  );
};
