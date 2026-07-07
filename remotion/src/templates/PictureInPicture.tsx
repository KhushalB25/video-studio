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
 * PICTURE IN PICTURE — a full-bleed main image with a small inset "window"
 * image (default bottom-right) that springs in a beat later. Use when the
 * speaker references a main visual plus a secondary reference at the same
 * time (a dashboard + a detail crop, a screen + a reaction shot). Differs
 * from image_compare_slider (wipe reveal between two images) — here both
 * images are visible simultaneously, one inset over the other.
 */
export type PictureInPictureProps = {
  main_image: string;
  pip_image: string;
  pip_label?: string;
  pip_corner?: "bottom-right" | "bottom-left" | "top-right" | "top-left";
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

const resolveSrc = (s: string): string => /^https?:\/\//i.test(s) ? s : staticFile(s);

export const PictureInPicture: React.FC<PictureInPictureProps> = ({
  main_image, pip_image, pip_label, pip_corner,
}) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const pipScale = spring({
    frame: Math.max(frame - 12, 0),
    fps,
    config: { damping: 16, stiffness: 150, mass: 0.7 },
  });

  const corner = pip_corner ?? "bottom-right";
  const pipW = width * 0.32;
  const pipH = pipW * 0.72;
  const margin = width * 0.05;
  const pos: React.CSSProperties = {};
  if (corner.includes("bottom")) pos.bottom = margin; else pos.top = margin;
  if (corner.includes("right")) pos.right = margin; else pos.left = margin;

  return (
    <AbsoluteFill style={{ backgroundColor: RAISIN }}>
      <Img src={resolveSrc(main_image)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
      <div style={{
        position: "absolute", ...pos,
        width: pipW, height: pipH,
        transform: `scale(${pipScale})`,
        transformOrigin: corner.includes("bottom") ? (corner.includes("right") ? "bottom right" : "bottom left")
          : (corner.includes("right") ? "top right" : "top left"),
        borderRadius: width * 0.02,
        overflow: "hidden",
        border: `2px solid ${LIME}`,
        boxShadow: "0 12px 36px rgba(0,0,0,0.55)",
        backgroundColor: RAISIN,
      }}>
        <Img src={resolveSrc(pip_image)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
        {pip_label && (
          <div style={{
            position: "absolute", left: 8, top: 8,
            fontFamily: BLOCK, fontWeight: 800, fontSize: typeBase * 0.018,
            color: RAISIN, background: LIME, padding: "3px 8px", borderRadius: 4,
            textTransform: "uppercase", letterSpacing: "0.04em",
          }}>
            {pip_label}
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};
