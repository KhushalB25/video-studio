import {
  AbsoluteFill,
  Img,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * LOGO REVEAL HERO — one big logo doing a proper cinematic reveal (3D-ish
 * spin settle + name slide-up), not a scattered multi-brand popup. Ported
 * from remotion-templates' logo-spin-reveal.tsx, rebuilt for our asset
 * pipeline (staticFile resolution) and brand palette.
 *
 * Use for a SINGLE hero product/brand moment that deserves the whole frame —
 * a launch announcement, "and here's the tool" reveal — not for enumerating
 * multiple brands (use tool_logo_burst for that).
 */
export type LogoRevealHeroProps = {
  image_path: string;
  name?: string;
  tagline?: string;
  beat_start_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

const resolveSrc = (p: string): string =>
  p.startsWith("http://") || p.startsWith("https://") || p.startsWith("data:")
    ? p : staticFile(p);

export const LogoRevealHero: React.FC<LogoRevealHeroProps> = ({
  image_path, name, tagline,
}) => {
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const spinProgress = spring({ frame, fps, config: { damping: 14, stiffness: 60, mass: 1 } });
  const rotateY = 90 * (1 - spinProgress);
  const logoOpacity = spinProgress;
  const logoScale = interpolate(spinProgress, [0, 1], [0.6, 1]);

  const textProgress = spring({
    frame: Math.max(0, frame - 18), fps,
    config: { damping: 13, stiffness: 90, mass: 0.6 },
  });

  const exitStart = durationInFrames - 8;
  const groupOp = frame > exitStart
    ? interpolate(frame, [exitStart, durationInFrames], [1, 0],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : 1;

  const logoSize = Math.round(width * 0.30);

  return (
    <AbsoluteFill style={{
      pointerEvents: "none", opacity: groupOp,
      display: "flex", alignItems: "center", justifyContent: "center",
      flexDirection: "column",
    }}>
      <div style={{
        width: logoSize, height: logoSize,
        borderRadius: Math.round(logoSize * 0.18),
        background: "#FFFFFF",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: logoSize * 0.14,
        boxSizing: "border-box",
        opacity: logoOpacity,
        transform: `perspective(800px) rotateY(${rotateY}deg) scale(${logoScale})`,
        boxShadow: "0 20px 60px rgba(0,0,0,0.6), 0 0 0 2px rgba(207,255,5,0.35)",
      }}>
        <Img src={resolveSrc(image_path)} style={{ width: "100%", height: "100%", objectFit: "contain" }} />
      </div>
      {name && (
        <div style={{
          marginTop: Math.round(typeBase * 0.045),
          opacity: textProgress,
          transform: `translateY(${(1 - textProgress) * 24}px)`,
          fontFamily: BLOCK, fontWeight: 900, fontSize: Math.round(typeBase * 0.06),
          color: "#FFFFFF", textAlign: "center",
          textShadow: "0 10px 30px rgba(0,0,0,0.85)",
        }}>
          {name}
        </div>
      )}
      {tagline && (
        <div style={{
          marginTop: Math.round(typeBase * 0.015),
          opacity: interpolate(textProgress, [0.5, 1], [0, 1], { extrapolateLeft: "clamp" }),
          fontFamily: BLOCK, fontWeight: 600, fontSize: Math.round(typeBase * 0.024),
          color: LIME, textAlign: "center", letterSpacing: "0.04em",
        }}>
          {tagline}
        </div>
      )}
    </AbsoluteFill>
  );
};
