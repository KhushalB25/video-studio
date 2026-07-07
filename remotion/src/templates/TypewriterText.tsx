import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import { useTypeBase } from "./motion";

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export type TypewriterTextProps = {
  text: string;
  chars_per_second?: number;
  vertical?: number;
  beat_start_sec?: number;
};

/**
 * TYPEWRITER TEXT — character-by-character typing reveal with a blinking
 * lime cursor. Differs from TextHighlightSweep (full text visible, sweep
 * highlights on top): here characters literally aren't rendered until
 * their turn arrives, driven by a chars-per-second typing rate rather than
 * a per-word stagger. Use for terminal/caption-style reveals, quotes being
 * "typed out" live.
 *
 * Single lime accent: only the blinking cursor bar is lime; typed text is
 * plain white.
 */
export const TypewriterText: React.FC<TypewriterTextProps> = ({
  text, chars_per_second, vertical, beat_start_sec,
}) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const cps = chars_per_second ?? 12;

  const t = Math.max(0, frame / fps);
  const visibleCount = Math.min(text.length, Math.floor(t * cps));
  const visibleText = text.slice(0, visibleCount);

  const cursorOn = Math.floor(frame / (fps * 0.5)) % 2 === 0;
  const fontSize = Math.round(typeBase * 0.048);
  const cy = Math.round(height * (vertical ?? 0.5));

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div style={{
        position: "absolute",
        left: Math.round(width * 0.1), right: Math.round(width * 0.1),
        top: cy - fontSize,
        textAlign: "center",
        fontFamily: BLOCK, fontWeight: 600, fontSize,
        color: "#FFFFFF",
        lineHeight: 1.35,
        letterSpacing: "0.01em",
      }}>
        {visibleText}
        <span style={{
          display: "inline-block",
          width: Math.round(fontSize * 0.08),
          height: fontSize * 0.9,
          marginLeft: Math.round(fontSize * 0.06),
          backgroundColor: LIME,
          opacity: cursorOn ? 1 : 0,
          verticalAlign: "text-bottom",
        }} />
      </div>
    </AbsoluteFill>
  );
};
