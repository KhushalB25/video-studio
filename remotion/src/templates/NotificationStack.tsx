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

const resolveSrc = (s: string): string => /^https?:\/\//i.test(s) ? s : staticFile(s);

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";
const MAX_TOASTS = 4;

export type NotificationStackItem = {
  app_name: string;
  title: string;
  body?: string;
  time?: string;
  app_icon?: string;
};

export type NotificationStackProps = {
  notifications: NotificationStackItem[];
  beat_start_sec?: number;
};

/**
 * NOTIFICATION STACK — multiple (2-4) compact toast cards cascading in from
 * the right, stacked top-to-bottom, with a lime unread-count badge on the
 * top card. This is CLEARLY DIFFERENT from `NotificationToast.tsx`: that
 * component is one large single toast that slides down from above. This one
 * is a pile of several small toasts arriving in a staggered burst from the
 * right edge — use when the beat is "notifications kept coming in" / "my
 * phone blew up" rather than "I got one notification saying X".
 *
 * Choreography (relative to beat_start_sec):
 *   0.00s   first (topmost) toast slides in from the right, springs to rest
 *   +0.25s  second toast slides in beneath it
 *   +0.50s  third toast slides in beneath that
 *   +0.75s  fourth toast slides in (if present)
 *
 * Hard rules:
 *  - Max 4 toasts rendered (excess items ignored)
 *  - Each card is compact (short height) so 4 stack without overflowing
 *    the frame
 *  - Unread badge (lime circle, count) sits on the top-most card only —
 *    single lime accent per frame stays intact (badge is the one lime
 *    element; icon squares stay raisin)
 *  - Title clamps 1 line, body clamps 1 line (shorter than the single-toast
 *    version since these cards are smaller)
 */
export const NotificationStack: React.FC<NotificationStackProps> = ({
  notifications,
  beat_start_sec,
}) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  if (!notifications || notifications.length === 0) return null;
  const items = notifications.slice(0, MAX_TOASTS);

  const isLandscape = width >= height;
  const cardW = Math.round(width * (isLandscape ? 0.30 : 0.74));
  const cardH = Math.round(typeBase * 0.11);
  const cardGap = Math.round(typeBase * 0.018);
  const margin = Math.round(typeBase * 0.036);
  const cardLeft = width - cardW - margin;
  const topStart = margin;

  const staggerSec = 0.25;
  const iconSize = Math.round(cardH * 0.5);
  const titleSize = Math.round(typeBase * 0.024);
  const bodySize = Math.round(typeBase * 0.020);
  const appSize = Math.round(typeBase * 0.017);

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      {items.map((n, i) => {
        const appearSec = i * staggerSec;
        const appearFrame = Math.round(appearSec * fps);
        const enter = spring({
          frame: frame - appearFrame, fps,
          durationInFrames: Math.round(0.5 * fps),
          config: { damping: 16, stiffness: 150, mass: 0.7 },
        });
        const visible = frame >= appearFrame;
        const cardTop = topStart + i * (cardH + cardGap);
        const slideX = interpolate(enter, [0, 1], [cardW * 0.6, 0]);

        return (
          <div key={i} style={{
            position: "absolute",
            left: cardLeft,
            top: cardTop,
            width: cardW,
            height: cardH,
            backgroundColor: "rgba(245,247,248,0.94)",
            backdropFilter: "blur(12px)",
            WebkitBackdropFilter: "blur(12px)",
            borderRadius: Math.round(typeBase * 0.018),
            border: "1px solid rgba(255,255,255,0.55)",
            boxShadow: `0 ${Math.round(typeBase * 0.012)}px ${Math.round(typeBase * 0.03)}px rgba(0,0,0,0.28)`,
            opacity: visible ? enter : 0,
            transform: `translateX(${slideX}px)`,
            display: "flex",
            alignItems: "center",
            gap: Math.round(typeBase * 0.012),
            padding: `0 ${Math.round(typeBase * 0.016)}px`,
          }}>
            <div style={{
              width: iconSize, height: iconSize,
              borderRadius: Math.round(iconSize * 0.24),
              backgroundColor: n.app_icon ? "transparent" : RAISIN,
              flexShrink: 0, overflow: "hidden", position: "relative",
            }}>
              {n.app_icon ? (
                <Img src={resolveSrc(n.app_icon)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
              ) : (
                <div style={{
                  position: "absolute", inset: 0, display: "flex",
                  alignItems: "center", justifyContent: "center",
                  fontFamily: BLOCK, fontWeight: 700, fontSize: Math.round(iconSize * 0.55),
                  color: "#FFFFFF", lineHeight: 1,
                }}>
                  ✦
                </div>
              )}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{
                display: "flex", alignItems: "baseline", justifyContent: "space-between",
                gap: Math.round(typeBase * 0.008),
              }}>
                <span style={{
                  fontFamily: BLOCK, fontWeight: 600, fontSize: appSize, color: "#5A6275",
                  textTransform: "uppercase", letterSpacing: "0.07em",
                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}>
                  {n.app_name}
                </span>
                {n.time && (
                  <span style={{ fontFamily: BLOCK, fontWeight: 500, fontSize: appSize, color: "#9AA3AB", flexShrink: 0 }}>
                    {n.time}
                  </span>
                )}
              </div>
              <div style={{
                fontFamily: BLOCK, fontWeight: 700, fontSize: titleSize, color: RAISIN,
                lineHeight: 1.15, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              }}>
                {n.title}
              </div>
              {n.body && (
                <div style={{
                  fontFamily: BLOCK, fontWeight: 500, fontSize: bodySize, color: "#343E5B",
                  lineHeight: 1.15, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}>
                  {n.body}
                </div>
              )}
            </div>
            {/* Unread badge — top card only */}
            {i === 0 && items.length > 1 && (
              <div style={{
                position: "absolute",
                top: -Math.round(cardH * 0.14),
                right: -Math.round(cardH * 0.14),
                minWidth: Math.round(cardH * 0.30),
                height: Math.round(cardH * 0.30),
                borderRadius: Math.round(cardH * 0.15),
                backgroundColor: LIME,
                display: "flex", alignItems: "center", justifyContent: "center",
                padding: `0 ${Math.round(cardH * 0.06)}px`,
                fontFamily: BLOCK, fontWeight: 800, fontSize: Math.round(cardH * 0.16),
                color: RAISIN,
                boxShadow: "0 2px 6px rgba(0,0,0,0.30)",
                opacity: visible ? enter : 0,
              }}>
                {items.length}
              </div>
            )}
          </div>
        );
      })}
    </AbsoluteFill>
  );
};
