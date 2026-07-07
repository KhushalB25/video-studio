import {
  AbsoluteFill,
  Img,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * IMAGE COMPARE SLIDER — before/after image with an animated wipe divider
 * sweeping left-to-right, revealing the "after" image underneath. Ported
 * from remotion-templates' image-comparison-slider.tsx, rebuilt into our
 * glassy image_card-style bottom-half card so the speaker stays visible.
 *
 * Use when the speaker describes a visual transformation ("before this / now
 * it looks like this") and you have TWO real images — screenshots of an old
 * vs new UI, before/after photos. For numeric before/after (a stat that
 * changed), use bar_overlay or ratio_dots instead — this is for images.
 */
export type ImageCompareSliderProps = {
  before_image: string;
  after_image: string;
  before_label?: string;
  after_label?: string;
  beat_start_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

const resolveSrc = (p: string): string =>
  p.startsWith("http://") || p.startsWith("https://") || p.startsWith("data:")
    ? p : staticFile(p);

export const ImageCompareSlider: React.FC<ImageCompareSliderProps> = ({
  before_image, after_image, before_label, after_label,
}) => {
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const exitStart = durationInFrames - 8;
  const groupOp = frame > exitStart
    ? interpolate(frame, [exitStart, durationInFrames], [1, 0],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : Math.min(1, frame / 8);

  // Wipe sweeps 0 -> 100% over the first ~1.6s, holds, then settles at 50%
  // so both sides stay readable for the rest of the beat.
  const sweepFrames = Math.round(1.6 * fps);
  const wipePct = interpolate(frame, [4, sweepFrames, sweepFrames + Math.round(0.4 * fps)],
    [0, 100, 50], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });

  const margin = width * 0.06;
  const cardTop = height * 0.58;
  const cardBottom = height * 0.04;
  const radius = width * 0.05;

  return (
    <AbsoluteFill style={{ pointerEvents: "none", opacity: groupOp }}>
      <div style={{
        position: "absolute", left: margin, right: margin, top: cardTop, bottom: cardBottom,
        borderRadius: radius, overflow: "hidden",
        border: `2px solid rgba(207,255,5,0.5)`,
        boxShadow: "0 20px 50px rgba(0,0,0,0.6)",
        backgroundColor: RAISIN,
      }}>
        <Img src={resolveSrc(after_image)} style={{ width: "100%", height: "100%", objectFit: "cover", position: "absolute", inset: 0 }} />
        <div style={{
          position: "absolute", inset: 0,
          clipPath: `inset(0 ${100 - wipePct}% 0 0)`,
        }}>
          <Img src={resolveSrc(before_image)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
        </div>
        {/* divider line + handle */}
        <div style={{
          position: "absolute", top: 0, bottom: 0, left: `${wipePct}%`,
          width: 3, background: LIME, transform: "translateX(-50%)",
          boxShadow: "0 0 12px rgba(207,255,5,0.7)",
        }} />
        <div style={{
          position: "absolute", top: "50%", left: `${wipePct}%`,
          width: Math.round(typeBase * 0.06), height: Math.round(typeBase * 0.06),
          transform: "translate(-50%,-50%)",
          borderRadius: "50%", background: LIME,
          display: "flex", alignItems: "center", justifyContent: "center",
          boxShadow: "0 4px 16px rgba(0,0,0,0.5)",
        }}>
          <svg width="40%" height="40%" viewBox="0 0 24 24" fill="none">
            <path d="M9 6l-6 6 6 6M15 6l6 6-6 6" stroke={RAISIN} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>
        {before_label && (
          <div style={{
            position: "absolute", left: 12, top: 12,
            fontFamily: BLOCK, fontWeight: 800, fontSize: Math.round(typeBase * 0.02),
            color: "#FFFFFF", background: "rgba(0,0,0,0.6)", padding: "4px 10px", borderRadius: 6,
            textTransform: "uppercase", letterSpacing: "0.05em",
            opacity: wipePct > 15 ? 1 : 0,
          }}>
            {before_label}
          </div>
        )}
        {after_label && (
          <div style={{
            position: "absolute", right: 12, top: 12,
            fontFamily: BLOCK, fontWeight: 800, fontSize: Math.round(typeBase * 0.02),
            color: RAISIN, background: LIME, padding: "4px 10px", borderRadius: 6,
            textTransform: "uppercase", letterSpacing: "0.05em",
            opacity: wipePct < 85 ? 1 : 0,
          }}>
            {after_label}
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};
