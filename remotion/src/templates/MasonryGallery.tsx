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
 * MASONRY GALLERY — Pinterest-style staggered multi-column grid where images
 * distribute round-robin into 3 columns with varied block heights, each
 * scaling+fading in with its own spring delay. Use for a denser, more
 * organic "lots of visuals" moment than gallery_grid's even 2x3 layout.
 */
export type MasonryGalleryProps = {
  images: string[];
  caption?: string;
};

const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

const resolveSrc = (s: string): string => /^https?:\/\//i.test(s) ? s : staticFile(s);

// Fixed height-fraction cadence so blocks look organically uneven without
// needing per-image metadata.
const HEIGHT_CYCLE = [0.42, 0.56, 0.48, 0.38, 0.6, 0.44];

export const MasonryGallery: React.FC<MasonryGalleryProps> = ({ images, caption }) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const items = (images || []).slice(0, 9);
  const cols: { src: string; h: number; idx: number }[][] = [[], [], []];
  items.forEach((src, i) => {
    cols[i % 3].push({ src, h: HEIGHT_CYCLE[i % HEIGHT_CYCLE.length], idx: i });
  });

  const gap = width * 0.018;
  const padX = width * 0.05;
  const padTop = height * 0.08;
  const padBottom = caption ? height * 0.14 : height * 0.06;

  return (
    <AbsoluteFill style={{ backgroundColor: RAISIN }}>
      <div style={{
        position: "absolute",
        left: padX, right: padX, top: padTop, bottom: padBottom,
        display: "flex", gap,
      }}>
        {cols.map((col, ci) => (
          <div key={ci} style={{ flex: 1, display: "flex", flexDirection: "column", gap }}>
            {col.map(({ src, h, idx }) => {
              const delay = idx * 3;
              const s = spring({
                frame: Math.max(frame - delay, 0),
                fps,
                config: { damping: 15, stiffness: 140, mass: 0.7 },
              });
              return (
                <div key={idx} style={{
                  height: `${h * 100}%`,
                  borderRadius: width * 0.02,
                  overflow: "hidden",
                  opacity: s,
                  transform: `scale(${0.85 + s * 0.15})`,
                  border: idx === 0 ? "2px solid rgba(207,255,5,0.55)" : "2px solid rgba(255,255,255,0.08)",
                  backgroundColor: RAISIN,
                }}>
                  <Img src={resolveSrc(src)} style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
                </div>
              );
            })}
          </div>
        ))}
      </div>
      {caption && (
        <div style={{
          position: "absolute", left: padX, right: padX, bottom: height * 0.05,
          textAlign: "center", fontFamily: BLOCK, fontWeight: 700,
          fontSize: typeBase * 0.036, color: "#FFFFFF",
        }}>
          {caption}
        </div>
      )}
    </AbsoluteFill>
  );
};
