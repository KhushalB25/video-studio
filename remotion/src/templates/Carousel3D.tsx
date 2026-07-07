import {
  AbsoluteFill,
  Img,
  staticFile,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useTypeBase } from "./motion";

const resolveSrc = (s: string): string => /^https?:\/\//i.test(s) ? s : staticFile(s);

const LIME = "#CFFF05";
const RAISIN = "#0F121A";
const BLOCK = "'Space Grotesk', system-ui, sans-serif";
const MAX_ITEMS = 8;

export type Carousel3DItem = {
  label: string;
  image_path?: string;
};

export type Carousel3DProps = {
  items: Carousel3DItem[];
  title?: string;
  beat_start_sec?: number;
};

/**
 * CAROUSEL 3D — a ring of cards arranged around a vertical axis in 3D
 * perspective, slowly orbiting over the whole beat. The front-facing card is
 * fully lit and scaled up; cards rotated away toward the sides/back dim and
 * shrink. Use for "here are our options / features / products" beats where a
 * flat grid would feel static — the orbit gives a sense of a catalog you're
 * spinning through.
 *
 * Choreography (relative to beat_start_sec):
 *   whole ring rotates continuously from 0deg to a fraction of a full turn
 *   over the beat duration (not a snap-to-card carousel — a smooth drift),
 *   so whichever card starts in front slowly cedes the spotlight to its
 *   neighbour.
 *
 * Hard rules:
 *  - Max 8 items rendered (a ring beyond that gets visually cluttered)
 *  - CSS 3D only: perspective on wrapper, rotateY + translateZ per card
 *  - Front-facing card (smallest abs angle to camera) gets full opacity/scale;
 *    dimming and scale-down are continuous functions of angle, not stepped
 *  - Single lime accent: only the placeholder-chip background (when no
 *    image_path) uses lime; labels stay white/gray
 */
export const Carousel3D: React.FC<Carousel3DProps> = ({
  items,
  title,
  beat_start_sec,
}) => {
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const localFrame = frame;

  if (!items || items.length === 0) return null;
  const N = Math.min(MAX_ITEMS, items.length);
  const list = items.slice(0, N);

  const cardW = Math.round(typeBase * 0.24);
  const cardH = Math.round(cardW * 1.15);
  const radius = Math.round(cardW * 1.5);
  const cx = Math.round(width / 2);
  const cy = Math.round(height * (title ? 0.56 : 0.5));

  // Slow continuous orbit: 0.55 of a full turn across the whole beat.
  const totalTurnDeg = 360 * 0.55;
  const ringRotation = interpolate(localFrame, [0, durationInFrames], [0, totalTurnDeg], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });

  const entrance = interpolate(localFrame, [0, Math.round(0.4 * fps)], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });

  const labelSize = Math.round(typeBase * 0.020);
  const titleSize = Math.round(typeBase * 0.034);

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      {title && (
        <div style={{
          position: "absolute", left: 0, right: 0,
          top: Math.round(height * 0.10), textAlign: "center",
          fontFamily: BLOCK, fontWeight: 800, fontSize: titleSize,
          color: "#FFFFFF", textTransform: "uppercase", letterSpacing: "0.06em",
          textShadow: "0 4px 14px rgba(0,0,0,0.85)",
          opacity: entrance,
        }}>
          {title}
        </div>
      )}
      <div style={{
        position: "absolute",
        left: cx - cardW / 2,
        top: cy - cardH / 2,
        width: cardW,
        height: cardH,
        perspective: 1800,
        opacity: entrance,
      }}>
        <div style={{
          position: "relative",
          width: "100%",
          height: "100%",
          transformStyle: "preserve-3d",
        }}>
          {list.map((it, i) => {
            const itemAngle = (360 / N) * i;
            const totalAngle = (itemAngle + ringRotation) % 360;
            // Normalize to [-180, 180] for "distance from front" (0deg = camera-facing).
            const normAngle = ((totalAngle + 180) % 360) - 180;
            const dist = Math.abs(normAngle) / 180; // 0 = front, 1 = back
            const scale = interpolate(dist, [0, 1], [1.0, 0.55]);
            const op = interpolate(dist, [0, 0.5, 1], [1, 0.55, 0.25]);
            const front = dist < 0.12;

            return (
              <div key={i} style={{
                position: "absolute",
                inset: 0,
                transform: `rotateY(${itemAngle + ringRotation}deg) translateZ(${radius}px) scale(${scale})`,
                transformStyle: "preserve-3d",
                opacity: op,
              }}>
                <div style={{
                  width: "100%",
                  height: "100%",
                  borderRadius: Math.round(typeBase * 0.018),
                  backgroundColor: RAISIN,
                  border: front ? `2px solid ${LIME}` : "2px solid rgba(255,255,255,0.10)",
                  boxShadow: front
                    ? `0 ${Math.round(typeBase * 0.02)}px ${Math.round(typeBase * 0.05)}px rgba(0,0,0,0.45)`
                    : `0 ${Math.round(typeBase * 0.01)}px ${Math.round(typeBase * 0.03)}px rgba(0,0,0,0.30)`,
                  overflow: "hidden",
                  display: "flex",
                  flexDirection: "column",
                }}>
                  <div style={{ flex: 1, position: "relative" }}>
                    {it.image_path ? (
                      <Img src={resolveSrc(it.image_path)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                    ) : (
                      <div style={{
                        position: "absolute", inset: 0,
                        backgroundColor: LIME,
                        display: "flex", alignItems: "center", justifyContent: "center",
                        fontFamily: BLOCK, fontWeight: 900,
                        fontSize: Math.round(cardW * 0.32),
                        color: RAISIN,
                      }}>
                        {it.label.charAt(0).toUpperCase()}
                      </div>
                    )}
                  </div>
                  <div style={{
                    padding: `${Math.round(cardH * 0.05)}px ${Math.round(cardW * 0.06)}px`,
                    fontFamily: BLOCK, fontWeight: 700, fontSize: labelSize,
                    color: "#E9ECED", textAlign: "center",
                    whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                    backgroundColor: RAISIN,
                  }}>
                    {it.label}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </AbsoluteFill>
  );
};
