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
 * SPLIT PANELS — two free-form image panels sliding in from the left/right
 * edges to meet at a center divider. Use for "two things side by side" where
 * both are images (two products, two scenes) with no data/label comparison
 * needed. NOT for enumerated comparisons (top_items/bottom_items) — that's
 * vs_split. This is purely two images meeting at a seam.
 */
export type SplitPanelsProps = {
  left_image: string;
  right_image: string;
  left_label?: string;
  right_label?: string;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

const resolveSrc = (s: string): string => /^https?:\/\//i.test(s) ? s : staticFile(s);

export const SplitPanels: React.FC<SplitPanelsProps> = ({
  left_image, right_image, left_label, right_label,
}) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const leftSlide = spring({ frame, fps, config: { damping: 16, stiffness: 110, mass: 0.8 } });
  const rightSlide = spring({ frame: frame - 5, fps, config: { damping: 16, stiffness: 110, mass: 0.8 } });
  const leftTx = interpolate(leftSlide, [0, 1], [-100, 0]);
  const rightTx = interpolate(rightSlide, [0, 1], [100, 0]);
  const dividerOpacity = interpolate(frame, [fps * 0.5, fps * 0.8], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });

  const labelStyle: React.CSSProperties = {
    fontFamily: BLOCK, fontWeight: 800, fontSize: typeBase * 0.022,
    color: "#FFFFFF", background: "rgba(0,0,0,0.55)", padding: "6px 14px",
    borderRadius: 6, textTransform: "uppercase", letterSpacing: "0.05em",
  };

  return (
    <AbsoluteFill style={{ backgroundColor: RAISIN, display: "flex" }}>
      <div style={{
        width: "50%", height: "100%", position: "relative", overflow: "hidden",
        transform: `translateX(${leftTx}%)`,
      }}>
        <Img src={resolveSrc(left_image)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
        {left_label && <div style={{ position: "absolute", left: 16, bottom: 16, ...labelStyle }}>{left_label}</div>}
      </div>
      <div style={{
        width: "50%", height: "100%", position: "relative", overflow: "hidden",
        transform: `translateX(${rightTx}%)`,
      }}>
        <Img src={resolveSrc(right_image)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
        {right_label && <div style={{ position: "absolute", right: 16, bottom: 16, ...labelStyle }}>{right_label}</div>}
      </div>
      <div style={{
        position: "absolute", top: 0, bottom: 0, left: "50%",
        transform: "translateX(-50%)", width: 3, background: LIME,
        opacity: dividerOpacity, boxShadow: "0 0 16px rgba(207,255,5,0.7)",
      }} />
    </AbsoluteFill>
  );
};
