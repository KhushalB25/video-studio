import { AbsoluteFill, useCurrentFrame, useVideoConfig, spring, interpolate } from "remotion";
import { useTypeBase } from "./motion";

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";

export type ListRevealItem = {
  text: string;
  appear_sec?: number;
};

export type ListRevealProps = {
  items: ListRevealItem[];
  title?: string;
  beat_start_sec?: number;
};

const MAX_ITEMS = 6;

/**
 * LIST REVEAL — compact stagger-reveal row list, scale+fade in per item
 * (0.85 → 1.0 spring pop). This is the "quick punchy list" sibling of
 * BulletedList: BulletedList is the official glyph-chip checklist
 * (check/x/dot/arrow semantics, 2-line wrap, heavier visual weight).
 * ListReveal drops the glyph-chip entirely in favor of a small lime index
 * number, and rows are compact pills meant for short phrases (feature
 * names, single words) rather than full sentences.
 *
 * MULTI_ITEM kind: items stagger across the whole beat (slot-preallocated,
 * spring per item reusing BulletedList's timing pattern) — informational
 * only, no extra config needed here.
 *
 * Single lime accent: only the index number/dot is lime; row bg and text
 * stay raisin/white.
 */
export const ListReveal: React.FC<ListRevealProps> = ({ items, title, beat_start_sec }) => {
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const N = Math.min(MAX_ITEMS, items.length);

  const padX = Math.round(width * (width >= height ? 0.18 : 0.10));
  const padY = Math.round(height * (title ? 0.22 : 0.14));
  const padBottom = Math.round(height * 0.10);
  const containerW = width - padX * 2;
  const containerH = height - padY - padBottom;

  const titleSize = Math.round(typeBase * 0.044);
  const itemSize = Math.round(typeBase * 0.036);
  const indexSize = Math.round(itemSize * 0.9);
  const rowGap = Math.round(typeBase * 0.018);
  const rowPadY = Math.round(typeBase * 0.016);
  const rowPadX = Math.round(typeBase * 0.024);

  const totalSec = durationInFrames / fps;
  const span = totalSec * 0.6;
  const norm = items.slice(0, MAX_ITEMS).map((it, i) => ({
    ...it,
    appear_sec: typeof it.appear_sec === "number" ? it.appear_sec : (span / Math.max(1, N)) * i,
  }));

  const titleEnter = spring({
    frame, fps, durationInFrames: 14,
    config: { damping: 16, stiffness: 140, mass: 0.7 },
  });

  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      {title && (
        <div style={{
          position: "absolute",
          left: padX, top: Math.round(height * 0.08),
          fontFamily: BLOCK, fontWeight: 800, fontSize: titleSize,
          color: "#FFFFFF", textTransform: "uppercase", letterSpacing: "0.05em",
          opacity: titleEnter,
          transform: `translateY(${interpolate(titleEnter, [0, 1], [-10, 0])}px)`,
        }}>
          {title}
        </div>
      )}
      <div style={{
        position: "absolute",
        left: padX, top: padY, width: containerW, height: containerH,
        display: "flex", flexDirection: "column", justifyContent: "center", gap: rowGap,
      }}>
        {norm.map((it, i) => {
          const itemFrame = Math.round(it.appear_sec! * fps);
          const enter = spring({
            frame: frame - itemFrame, fps,
            durationInFrames: Math.round(fps * 0.4),
            config: { damping: 15, stiffness: 160, mass: 0.7 },
          });
          const visible = frame >= itemFrame;
          const scale = 0.85 + 0.15 * enter;

          return (
            <div key={i} style={{
              // Slot pre-allocated (always rendered) so earlier rows never
              // shift when later rows appear.
              display: "flex", alignItems: "center", gap: Math.round(itemSize * 0.5),
              opacity: visible ? enter : 0,
              transform: visible ? `scale(${scale})` : "scale(0.85)",
              transformOrigin: "left center",
              backgroundColor: "rgba(15,18,26,0.55)",
              borderRadius: Math.round(itemSize * 0.5),
              padding: `${rowPadY}px ${rowPadX}px`,
            }}>
              <div style={{
                width: Math.round(indexSize * 1.7), height: Math.round(indexSize * 1.7),
                borderRadius: "50%",
                display: "flex", alignItems: "center", justifyContent: "center",
                fontFamily: BLOCK, fontWeight: 800, fontSize: indexSize,
                color: RAISIN, backgroundColor: LIME, flexShrink: 0,
              }}>
                {i + 1}
              </div>
              <div style={{
                fontFamily: BLOCK, fontWeight: 700, fontSize: itemSize,
                color: "#FFFFFF", lineHeight: 1.2,
                whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
              }}>
                {it.text}
              </div>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
