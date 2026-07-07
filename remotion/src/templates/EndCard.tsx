import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

/**
 * END CARD — a proper full-frame outro takeover: title + optional subtitle
 * + CTA line, on a raisin backdrop with a soft lime glow. Ported/rebuilt from
 * remotion-templates' end-card.tsx + credits-roll.tsx.
 *
 * Distinct from the `subscribe` kind (a small animated pill button that
 * overlays the speaker) — this is a full-screen closer for when the video
 * genuinely ends here (no more speaker footage after this beat). Use at the
 * very end of the timeline, after the speaker's last line.
 */
export type EndCardProps = {
  title: string;
  subtitle?: string;
  cta?: string;
  beat_start_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export const EndCard: React.FC<EndCardProps> = ({ title, subtitle, cta }) => {
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const enter = spring({ frame, fps, durationInFrames: Math.round(0.6 * fps),
    config: { damping: 16, stiffness: 110, mass: 0.9 } });
  const titleOp = interpolate(enter, [0, 1], [0, 1]);
  const titleY = interpolate(enter, [0, 1], [30, 0]);

  const subEnter = spring({ frame: Math.max(0, frame - 10), fps,
    durationInFrames: Math.round(0.5 * fps),
    config: { damping: 16, stiffness: 110, mass: 0.8 } });

  const ctaEnter = spring({ frame: Math.max(0, frame - 20), fps,
    durationInFrames: Math.round(0.5 * fps),
    config: { damping: 14, stiffness: 130, mass: 0.7 } });

  const glowPulse = 0.3 + 0.15 * Math.sin(frame / 20);

  return (
    <AbsoluteFill style={{
      backgroundColor: RAISIN,
      display: "flex", alignItems: "center", justifyContent: "center",
      flexDirection: "column",
    }}>
      <div style={{
        position: "absolute", width: width * 0.7, height: width * 0.7,
        borderRadius: "50%",
        background: `radial-gradient(circle, rgba(207,255,5,${glowPulse}) 0%, transparent 70%)`,
        filter: "blur(40px)",
      }} />
      <div style={{
        fontFamily: BLOCK, fontWeight: 900, fontSize: Math.round(typeBase * 0.075),
        color: "#FFFFFF", textAlign: "center", maxWidth: width * 0.85,
        opacity: titleOp, transform: `translateY(${titleY}px)`,
        textShadow: "0 10px 40px rgba(0,0,0,0.6)",
        lineHeight: 1.1,
      }}>
        {title}
      </div>
      {subtitle && (
        <div style={{
          marginTop: Math.round(typeBase * 0.02),
          fontFamily: BLOCK, fontWeight: 600, fontSize: Math.round(typeBase * 0.028),
          color: "#B5BFC2", textAlign: "center", maxWidth: width * 0.75,
          opacity: subEnter, transform: `translateY(${(1 - subEnter) * 20}px)`,
        }}>
          {subtitle}
        </div>
      )}
      {cta && (
        <div style={{
          marginTop: Math.round(typeBase * 0.05),
          opacity: ctaEnter, transform: `scale(${interpolate(ctaEnter, [0, 1], [0.85, 1])})`,
          background: LIME, color: RAISIN,
          fontFamily: BLOCK, fontWeight: 800, fontSize: Math.round(typeBase * 0.026),
          padding: `${Math.round(typeBase * 0.02)}px ${Math.round(typeBase * 0.045)}px`,
          borderRadius: 999,
          textTransform: "uppercase", letterSpacing: "0.05em",
          boxShadow: "0 10px 30px rgba(207,255,5,0.3)",
        }}>
          {cta}
        </div>
      )}
    </AbsoluteFill>
  );
};
