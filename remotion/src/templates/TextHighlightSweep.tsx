import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { useTypeBase } from "./motion";

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export type TextHighlightSweepProps = {
  text: string;
  beat_start_sec?: number;
};

/**
 * TEXT HIGHLIGHT SWEEP — word-by-word cumulative highlighter sweep, as if
 * a marker is moving under the text as it's spoken/read. Differs from
 * TypewriterText (which reveals characters that weren't visible before):
 * here the FULL text is visible from frame 0, and highlighting sweeps
 * across it left-to-right. Differs from PulseText (single continuous loop):
 * this is a one-shot progressive reveal driven by word index, not a loop.
 *
 * Cumulative: once a word is highlighted it STAYS highlighted (raisin text
 * on lime chip) as later words light up. Unread words stay gray/white on
 * transparent. Single lime accent: only the active/read highlight bg uses
 * lime.
 */
export const TextHighlightSweep: React.FC<TextHighlightSweepProps> = ({ text }) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();

  const words = text.split(/\s+/).filter(Boolean);
  const secPerWord = 0.25;
  const fontSize = Math.round(typeBase * 0.05);

  // frame from useCurrentFrame() inside this beat's <Sequence> is already
  // relative to the beat's own start (0 at beat start) — no beat_start_sec
  // offset needed here.
  const t = Math.max(0, frame / fps);

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div style={{
        position: "absolute",
        left: Math.round(width * 0.1), right: Math.round(width * 0.1),
        top: Math.round(height * 0.5) - fontSize,
        display: "flex", flexWrap: "wrap",
        justifyContent: "center", alignItems: "center",
        gap: Math.round(fontSize * 0.35),
      }}>
        {words.map((w, i) => {
          const wordStart = i * secPerWord;
          const prog = interpolate(t, [wordStart, wordStart + secPerWord * 0.6], [0, 1], {
            extrapolateLeft: "clamp", extrapolateRight: "clamp",
          });
          const read = prog >= 1;
          return (
            <span key={i} style={{
              position: "relative",
              fontFamily: BLOCK, fontWeight: 700, fontSize,
              lineHeight: 1.3,
              padding: `${Math.round(fontSize * 0.06)}px ${Math.round(fontSize * 0.12)}px`,
              borderRadius: Math.round(fontSize * 0.12),
              color: read ? RAISIN : "#B5BFC2",
              backgroundColor: read
                ? LIME
                : `rgba(207,255,5,${0.0 + 0.0 * prog})`,
              boxShadow: prog > 0 && prog < 1
                ? `inset ${Math.round(fontSize * prog)}px 0 0 0 ${LIME}`
                : undefined,
              overflow: "hidden",
              transition: "none",
            }}>
              {w}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
