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
 * GALLERY GRID — full-screen CSS grid (2x3) of images that scale+fade in with
 * a staggered spring, one cell at a time. Use for "here's a bunch of shots"
 * moments (a photo dump, a set of screenshots, a montage of examples).
 * Differs from masonry_gallery (uneven Pinterest-style block heights) and
 * photo_stack (3 overlapping polaroids, not a grid) — this is a clean,
 * evenly-sized grid.
 */
export type GalleryGridProps = {
  images: string[];
  caption?: string;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

const resolveSrc = (s: string): string => /^https?:\/\//i.test(s) ? s : staticFile(s);

export const GalleryGrid: React.FC<GalleryGridProps> = ({ images, caption }) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const cells = (images || []).slice(0, 6);
  const cols = cells.length > 4 ? 3 : 2;
  const rows = Math.ceil(cells.length / cols);
  const gap = width * 0.02;
  const padX = width * 0.05;
  const padTop = height * 0.08;
  const padBottom = caption ? height * 0.14 : height * 0.08;

  return (
    <AbsoluteFill style={{ backgroundColor: RAISIN }}>
      <div style={{
        position: "absolute",
        left: padX, right: padX, top: padTop, bottom: padBottom,
        display: "grid",
        gridTemplateColumns: `repeat(${cols}, 1fr)`,
        gridTemplateRows: `repeat(${rows}, 1fr)`,
        gap,
      }}>
        {cells.map((src, i) => {
          const delay = i * 4;
          const s = spring({
            frame: Math.max(frame - delay, 0),
            fps,
            config: { damping: 15, stiffness: 140, mass: 0.7 },
          });
          const scale = 0.82 + s * 0.18;
          return (
            <div key={i} style={{
              position: "relative",
              overflow: "hidden",
              borderRadius: width * 0.02,
              opacity: s,
              transform: `scale(${scale})`,
              backgroundColor: RAISIN,
              border: i === 0 ? `2px solid rgba(207,255,5,0.55)` : "2px solid rgba(255,255,255,0.08)",
            }}>
              <Img src={resolveSrc(src)} style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
            </div>
          );
        })}
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
