import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { useTypeBase } from "./motion";

/**
 * COMPARISON BARS — side-by-side before/after metric bars across multiple
 * rows, each row's pair animating in with a per-row stagger. Differs from
 * ProgressBars (one bar per row, independent metrics) and RingChart (parts
 * of a whole): this is explicitly a paired comparison ("before vs after"
 * for each row) — pricing changes, A/B results, quarter-over-quarter shifts.
 *
 * Single lime accent: only the "after" bar is lime; "before" stays a muted
 * raisin/gray tone so the contrast reads as improvement/change, not two
 * competing accents.
 */
export type ComparisonBarsProps = {
  rows: { label: string; before: number; after: number; max?: number }[];
  before_label?: string;
  after_label?: string;
  title?: string;
  beat_start_sec?: number;
};

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";
const MAX_ROWS = 5;

export const ComparisonBars: React.FC<ComparisonBarsProps> = ({
  rows, before_label, after_label, title, beat_start_sec,
}) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const f = frame;

  if (!rows || rows.length === 0) return null;
  const data = rows.slice(0, MAX_ROWS);

  const trackW = Math.round(width * 0.7);
  const trackX = Math.round((width - trackW) / 2);
  const barH = Math.round(typeBase * 0.026);
  const rowGap = Math.round(typeBase * 0.014);
  const rowH = Math.round(typeBase * 0.10);
  const headerY = Math.round(height * 0.20);
  const startY = Math.round(height * 0.28);
  const staggerFrames = Math.round(0.2 * fps);
  const fillFrames = Math.round(0.5 * fps);

  const headerEnter = spring({ frame: f, fps, durationInFrames: Math.round(0.4 * fps),
    config: { damping: 16, stiffness: 130, mass: 0.8 } });

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      {title && (
        <div style={{
          position: "absolute", left: trackX, top: Math.round(headerY - typeBase * 0.09),
          fontFamily: BLOCK, fontWeight: 800, fontSize: Math.round(typeBase * 0.032),
          color: "#FFFFFF", textTransform: "uppercase", letterSpacing: "0.06em",
          textShadow: "0 4px 14px rgba(0,0,0,0.85)",
          opacity: headerEnter,
        }}>
          {title}
        </div>
      )}
      <div style={{
        position: "absolute", left: trackX, top: headerY, width: trackW,
        display: "flex", gap: Math.round(typeBase * 0.02),
        opacity: headerEnter,
      }}>
        <div style={{
          display: "flex", alignItems: "center", gap: 6,
          fontFamily: BLOCK, fontWeight: 700, fontSize: Math.round(typeBase * 0.02),
          color: "#B5BFC2",
        }}>
          <span style={{ width: Math.round(typeBase * 0.014), height: Math.round(typeBase * 0.014),
            borderRadius: "50%", backgroundColor: RAISIN, border: "1px solid #3A4152", display: "inline-block" }} />
          {before_label ?? "Before"}
        </div>
        <div style={{
          display: "flex", alignItems: "center", gap: 6,
          fontFamily: BLOCK, fontWeight: 700, fontSize: Math.round(typeBase * 0.02),
          color: "#E9ECED",
        }}>
          <span style={{ width: Math.round(typeBase * 0.014), height: Math.round(typeBase * 0.014),
            borderRadius: "50%", backgroundColor: LIME, display: "inline-block" }} />
          {after_label ?? "After"}
        </div>
      </div>

      {data.map((r, i) => {
        const max = r.max ?? Math.max(r.before, r.after, 1);
        const beforePct = Math.max(0, Math.min(1, r.before / max));
        const afterPct = Math.max(0, Math.min(1, r.after / max));
        const appearF = staggerFrames * i;
        const rowSpring = spring({
          frame: f - appearF, fps, durationInFrames: fillFrames,
          config: { damping: 16, stiffness: 130, mass: 0.75 },
        });
        const y = startY + i * rowH;

        return (
          <div key={i} style={{
            position: "absolute", left: trackX, top: y, width: trackW,
            opacity: rowSpring,
          }}>
            <div style={{
              fontFamily: BLOCK, fontWeight: 700, fontSize: Math.round(typeBase * 0.022),
              color: "#E9ECED", marginBottom: Math.round(typeBase * 0.008),
            }}>
              {r.label}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: rowGap }}>
              <div style={{
                display: "flex", alignItems: "center", gap: Math.round(typeBase * 0.012),
              }}>
                <div style={{
                  width: trackW * 0.85, height: barH, borderRadius: Math.round(barH / 2),
                  backgroundColor: "rgba(255,255,255,0.06)", overflow: "hidden",
                }}>
                  <div style={{
                    width: Math.round(trackW * 0.85 * beforePct * rowSpring), height: barH,
                    borderRadius: Math.round(barH / 2), backgroundColor: "#3A4152",
                  }} />
                </div>
                <span style={{
                  fontFamily: BLOCK, fontWeight: 600, fontSize: Math.round(typeBase * 0.018), color: "#B5BFC2",
                }}>
                  {r.before}
                </span>
              </div>
              <div style={{
                display: "flex", alignItems: "center", gap: Math.round(typeBase * 0.012),
              }}>
                <div style={{
                  width: trackW * 0.85, height: barH, borderRadius: Math.round(barH / 2),
                  backgroundColor: RAISIN, overflow: "hidden",
                }}>
                  <div style={{
                    width: Math.round(trackW * 0.85 * afterPct * rowSpring), height: barH,
                    borderRadius: Math.round(barH / 2), backgroundColor: LIME,
                    boxShadow: "0 0 8px rgba(207,255,5,0.4)",
                  }} />
                </div>
                <span style={{
                  fontFamily: BLOCK, fontWeight: 700, fontSize: Math.round(typeBase * 0.018), color: "#E9ECED",
                }}>
                  {r.after}
                </span>
              </div>
            </div>
          </div>
        );
      })}
    </AbsoluteFill>
  );
};
