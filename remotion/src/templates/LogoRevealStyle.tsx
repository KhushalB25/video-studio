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
 * LOGO REVEAL STYLE — one logo/brand reveal moment, 8 alternate entrance
 * mechanics behind a single `style` switch. Ported from remotion-templates'
 * logo-blur-reveal / logo-bounce-drop / logo-fade-reveal / logo-glitch-reveal
 * / logo-scale-rotate / logo-split-reveal / logo-stroke-draw / logo-typewriter,
 * rebuilt for our asset pipeline (staticFile resolution) and brand palette.
 * Sibling to LogoRevealHero (the 3D spin-settle variant) — use this one when
 * you want a specific, less "hero cinematic" entrance feel instead.
 */
export type LogoRevealStyleProps = {
  image_path: string;
  name?: string;
  tagline?: string;
  style?:
    | "blur"
    | "bounce"
    | "fade"
    | "glitch"
    | "scale_rotate"
    | "split"
    | "stroke_draw"
    | "typewriter";
  beat_start_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

const resolveSrc = (s: string): string =>
  /^https?:\/\//i.test(s) ? s : staticFile(s);

export const LogoRevealStyle: React.FC<LogoRevealStyleProps> = ({
  image_path, name, tagline, style = "bounce",
}) => {
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const src = resolveSrc(image_path);

  const exitStart = durationInFrames - 8;
  const groupOp = frame > exitStart
    ? interpolate(frame, [exitStart, durationInFrames], [1, 0],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
    : 1;

  const logoSize = Math.round(width * 0.24);

  const NameBlock = ({ opacity, translateY }: { opacity: number; translateY: number }) => (
    <>
      {name && (
        <div style={{
          marginTop: Math.round(typeBase * 0.045),
          opacity,
          transform: `translateY(${translateY}px)`,
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
          opacity: interpolate(opacity, [0.5, 1], [0, 1], { extrapolateLeft: "clamp" }),
          fontFamily: BLOCK, fontWeight: 600, fontSize: Math.round(typeBase * 0.024),
          color: LIME, textAlign: "center", letterSpacing: "0.04em",
        }}>
          {tagline}
        </div>
      )}
    </>
  );

  const LogoBadge: React.FC<{ style?: React.CSSProperties }> = ({ style: extra }) => (
    <div style={{
      width: logoSize, height: logoSize,
      borderRadius: Math.round(logoSize * 0.18),
      background: "#FFFFFF",
      display: "flex", alignItems: "center", justifyContent: "center",
      padding: logoSize * 0.14,
      boxSizing: "border-box",
      boxShadow: "0 20px 60px rgba(0,0,0,0.6), 0 0 0 2px rgba(207,255,5,0.35)",
      ...extra,
    }}>
      <Img src={src} style={{ width: "100%", height: "100%", objectFit: "contain" }} />
    </div>
  );

  let body: React.ReactNode;

  if (style === "blur") {
    // logo-blur-reveal: blur 20->0 + opacity 0.3->1 over 1.5s, name springs in after
    const blur = interpolate(frame, [0, fps * 1.5], [20, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    const opacity = interpolate(frame, [0, fps * 1.5], [0.3, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    const nameProgress = spring({ frame: Math.max(0, frame - Math.round(fps * 1.5)), fps, config: { damping: 14, stiffness: 110, mass: 0.7 } });
    body = (
      <>
        <div style={{ filter: `blur(${blur}px)`, opacity }}>
          <LogoBadge />
        </div>
        <NameBlock opacity={nameProgress} translateY={(1 - nameProgress) * 20} />
      </>
    );
  } else if (style === "bounce") {
    // logo-bounce-drop: springs down from above, squash/stretch on landing
    const drop = spring({ frame, fps, config: { damping: 8, stiffness: 130, mass: 0.8 } });
    const translateY = interpolate(drop, [0, 1], [-200, 0]);
    const squash = spring({ frame: Math.max(0, frame - 8), fps, config: { damping: 6, stiffness: 180, mass: 0.6 } });
    const scaleX = interpolate(squash, [0, 0.5, 1], [1.3, 1.1, 1]);
    const scaleY = interpolate(squash, [0, 0.5, 1], [0.7, 0.9, 1]);
    const nameOpacity = interpolate(frame, [25, 40], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    const nameY = interpolate(frame, [25, 40], [20, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    body = (
      <>
        <div style={{ transform: `translateY(${translateY}px) scaleX(${scaleX}) scaleY(${scaleY})` }}>
          <LogoBadge />
        </div>
        <NameBlock opacity={nameOpacity} translateY={nameY} />
      </>
    );
  } else if (style === "fade") {
    // logo-fade-reveal: simple spring opacity+scale, name delayed
    const logoProgress = spring({ frame, fps, config: { damping: 14, stiffness: 110, mass: 0.8 } });
    const textProgress = spring({ frame: Math.max(0, frame - 15), fps, config: { damping: 16, stiffness: 90, mass: 0.6 } });
    body = (
      <>
        <div style={{ opacity: logoProgress, transform: `scale(${0.8 + 0.2 * logoProgress})` }}>
          <LogoBadge />
        </div>
        <NameBlock opacity={textProgress} translateY={20 * (1 - textProgress)} />
      </>
    );
  } else if (style === "glitch") {
    // logo-glitch-reveal: RGB channel-split offsets decaying to 0, clean logo settles in
    const decay = interpolate(frame, [0, 30], [1, 0], { extrapolateRight: "clamp" });
    const redX = Math.sin(frame * 7.3) * 15 * decay, redY = Math.sin(frame * 5.1) * 10 * decay;
    const greenX = Math.sin(frame * 11.7) * 15 * decay, greenY = Math.sin(frame * 3.9) * 10 * decay;
    const blueX = Math.sin(frame * 9.2) * 15 * decay, blueY = Math.sin(frame * 6.4) * 10 * decay;
    const cleanOpacity = spring({ frame: Math.max(0, frame - 25), fps, config: { damping: 14, stiffness: 110, mass: 0.7 } });
    const channelOpacity = interpolate(frame, [20, 35], [0.7, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    const glowIntensity = interpolate(frame, [20, 40], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    const chanStyle: React.CSSProperties = {
      position: "absolute", width: logoSize, height: logoSize,
      borderRadius: Math.round(logoSize * 0.18), display: "flex", alignItems: "center", justifyContent: "center",
    };
    body = (
      <>
        <div style={{ position: "relative", width: logoSize, height: logoSize }}>
          <div style={{ ...chanStyle, background: "rgba(239,68,68,0.5)", transform: `translate(${redX}px, ${redY}px)`, opacity: channelOpacity, mixBlendMode: "screen" }} />
          <div style={{ ...chanStyle, background: "rgba(34,197,94,0.5)", transform: `translate(${greenX}px, ${greenY}px)`, opacity: channelOpacity, mixBlendMode: "screen" }} />
          <div style={{ ...chanStyle, background: "rgba(59,130,246,0.5)", transform: `translate(${blueX}px, ${blueY}px)`, opacity: channelOpacity, mixBlendMode: "screen" }} />
          <div style={{ ...chanStyle, opacity: cleanOpacity, boxShadow: `0 0 ${40 * glowIntensity}px rgba(207,255,5,${0.4 * glowIntensity})` }}>
            <LogoBadge style={{ width: "100%", height: "100%" }} />
          </div>
        </div>
        <NameBlock opacity={cleanOpacity} translateY={(1 - cleanOpacity) * 16} />
      </>
    );
  } else if (style === "scale_rotate") {
    // logo-scale-rotate: spins 360deg while scaling in, pulsing glow after settle
    const entrance = spring({ frame, fps, config: { damping: 10, stiffness: 120, mass: 0.8 } });
    const scale = interpolate(entrance, [0, 1], [0, 1]);
    const rotation = interpolate(entrance, [0, 1], [0, 360]);
    const glow = frame > 20 ? 8 + Math.sin(frame * 0.15) * 4 : 0;
    const nameProgress = spring({ frame: Math.max(0, frame - 20), fps, config: { damping: 14, stiffness: 120, mass: 0.7 } });
    body = (
      <>
        <div style={{ transform: `scale(${scale}) rotate(${rotation}deg)` }}>
          <LogoBadge style={{ borderRadius: "50%", boxShadow: `0 0 ${glow}px ${glow / 2}px rgba(207,255,5,0.5)` }} />
        </div>
        <NameBlock opacity={nameProgress} translateY={interpolate(nameProgress, [0, 1], [30, 0])} />
      </>
    );
  } else if (style === "split") {
    // logo-split-reveal: two halves of the badge expand outward from center
    const revealProgress = interpolate(frame, [10, fps * 1.5], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    const halfW = Math.round(logoSize * 0.5 * revealProgress);
    const leftTranslate = -Math.round(logoSize * 0.5 * revealProgress);
    const rightTranslate = Math.round(logoSize * 0.5 * revealProgress);
    const nameOpacity = interpolate(frame, [fps * 1.6, fps * 2.2], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    const nameY = interpolate(frame, [fps * 1.6, fps * 2.2], [15, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    body = (
      <>
        <div style={{ position: "relative", height: logoSize, display: "flex", alignItems: "center" }}>
          <div style={{
            width: halfW, height: logoSize, overflow: "hidden",
            background: "#FFFFFF", borderRadius: `${Math.round(logoSize * 0.18)}px 0 0 ${Math.round(logoSize * 0.18)}px`,
            transform: `translateX(${leftTranslate}px)`,
            display: "flex", alignItems: "center", justifyContent: "flex-end",
          }}>
            <Img src={src} style={{ width: logoSize, height: logoSize, objectFit: "contain", opacity: revealProgress }} />
          </div>
          <div style={{
            width: halfW, height: logoSize, overflow: "hidden",
            background: "#FFFFFF", borderRadius: `0 ${Math.round(logoSize * 0.18)}px ${Math.round(logoSize * 0.18)}px 0`,
            transform: `translateX(${rightTranslate}px)`,
            display: "flex", alignItems: "center", justifyContent: "flex-start",
          }}>
            <Img src={src} style={{ width: logoSize, height: logoSize, objectFit: "contain", opacity: revealProgress, marginLeft: -logoSize }} />
          </div>
        </div>
        <NameBlock opacity={nameOpacity} translateY={nameY} />
      </>
    );
  } else if (style === "stroke_draw") {
    // logo-stroke-draw: hexagon + inner triangle draw via stroke-dasharray, then fill
    const hexPerimeter = 300, triPerimeter = 150;
    const hexOffset = interpolate(frame, [0, fps * 1.2], [hexPerimeter, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    const triOffset = interpolate(frame, [fps * 0.4, fps * 1.6], [triPerimeter, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    const fillOpacity = interpolate(frame, [fps * 1.6, fps * 2.2], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    const nameOpacity = interpolate(frame, [fps * 2.0, fps * 2.5], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    const svgSize = logoSize;
    const hexPoints = "60,10 103.3,35 103.3,85 60,110 16.7,85 16.7,35";
    const triPoints = "60,30 85.98,75 34.02,75";
    body = (
      <>
        <svg width={svgSize} height={svgSize} viewBox="0 0 120 120">
          <polygon points={hexPoints} fill={LIME} opacity={fillOpacity * 0.25} />
          <polygon points={hexPoints} fill="none" stroke={LIME} strokeWidth="2.5"
            strokeDasharray={hexPerimeter} strokeDashoffset={hexOffset} strokeLinejoin="round" />
          <polygon points={triPoints} fill="#FFFFFF" opacity={fillOpacity} />
          <polygon points={triPoints} fill="none" stroke={LIME} strokeWidth="2.5"
            strokeDasharray={triPerimeter} strokeDashoffset={triOffset} strokeLinejoin="round" />
        </svg>
        <div style={{ opacity: fillOpacity, marginTop: -Math.round(svgSize * 0.15) }}>
          <Img src={src} style={{ width: Math.round(svgSize * 0.4), height: Math.round(svgSize * 0.4), objectFit: "contain" }} />
        </div>
        <NameBlock opacity={nameOpacity} translateY={0} />
      </>
    );
  } else {
    // typewriter — logo-typewriter: icon pops via spring scale, then name types char-by-char with blinking cursor
    const text = (name ?? "").toUpperCase();
    const iconScale = spring({ frame, fps, config: { damping: 10, stiffness: 160, mass: 0.6 } });
    const typeStart = 15;
    const charsPerFrame = 0.4;
    const charsVisible = Math.min(Math.floor(Math.max(0, frame - typeStart) * charsPerFrame), text.length);
    const displayedText = text.slice(0, charsVisible);
    const cursorBlink = Math.floor(frame / 15) % 2 === 0;
    const showCursor = frame > typeStart && (charsVisible < text.length || cursorBlink);
    body = (
      <div style={{ display: "flex", alignItems: "center", gap: Math.round(typeBase * 0.02) }}>
        <div style={{ transform: `scale(${iconScale})` }}>
          <LogoBadge style={{ width: Math.round(logoSize * 0.7), height: Math.round(logoSize * 0.7) }} />
        </div>
        <div style={{ display: "flex", alignItems: "center" }}>
          <span style={{
            fontFamily: BLOCK, fontWeight: 900, fontSize: Math.round(typeBase * 0.06),
            color: "#FFFFFF", letterSpacing: "0.06em", whiteSpace: "pre",
            textShadow: "0 10px 30px rgba(0,0,0,0.85)",
          }}>
            {displayedText}
          </span>
          {showCursor && (
            <span style={{ fontFamily: BLOCK, fontWeight: 900, fontSize: Math.round(typeBase * 0.06), color: LIME, marginLeft: 2 }}>
              |
            </span>
          )}
        </div>
      </div>
    );
  }

  return (
    <AbsoluteFill style={{
      pointerEvents: "none", opacity: groupOp,
      display: "flex", alignItems: "center", justifyContent: "center",
      flexDirection: "column",
    }}>
      {body}
    </AbsoluteFill>
  );
};
