import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, Easing } from "remotion";
import { LightGridBg } from "./Backgrounds";
import { useFadeRise, useSpringIn, useSettleZoom, useTypeBase } from "./motion";

export type StatDeltaProps = {
  /** Optional small label above the metric (e.g. "MRR" / "DAILY USERS"). */
  pre_label?: string;
  /** Prefix attached to the rolling number ("$" / "+"). */
  prefix?: string;
  /** Numerical target the counter rolls TO. */
  target: number;
  /** Suffix attached after the number ("k", "%", "/mo", "x"). */
  suffix?: string;
  /** Number of decimal places to render. Default 0. */
  decimals?: number;
  /** Roll-up duration in seconds. Default 1.2s. */
  duration_sec?: number;
  /** Delta value shown in the chip, e.g. "12.5%" or "3.2k". */
  delta_value: string;
  /** Arrow direction — up = lime, down = neutral. Default "up". */
  delta_direction?: "up" | "down";
  /** Trailing label after the delta value, e.g. "This Month". */
  delta_label?: string;
  beat_start_sec?: number;
};

/**
 * STAT COUNTER — a MetricReveal-style count-up hero number, PLUS a delta
 * chip beneath it (▲ 12.5% This Month). MetricReveal is the plain hero
 * number; this adds the "and here's the trend" row, for stats that need
 * context beyond the raw magnitude (MRR that grew, retention that dropped).
 *
 * Single lime accent rule: only the UP delta (arrow + value) is lime; DOWN
 * uses a neutral muted tone, never lime, so direction reads without a
 * second accent color competing with the hero number treatment.
 */
export const StatCounter: React.FC<StatDeltaProps> = ({
  pre_label,
  prefix,
  target,
  suffix,
  decimals,
  duration_sec,
  delta_value,
  delta_direction,
  delta_label,
  beat_start_sec,
}) => {
  const { fps, width } = useVideoConfig();
  const frame = useCurrentFrame();
  const typeBase = useTypeBase();
  const fontFamily = "Space Grotesk, system-ui, sans-serif";

  const dur = duration_sec ?? 1.2;
  const decimalPlaces = decimals ?? 0;
  const startSec = 0.20;

  const k = interpolate(
    frame / fps,
    [startSec, startSec + dur],
    [0, 1],
    {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
      easing: Easing.bezier(0.20, 0.65, 0.20, 1.0),
    },
  );
  const current = target * k;
  const formatted = current.toFixed(decimalPlaces);
  const formattedHuman = (() => {
    const [intPart, decPart] = formatted.split(".");
    const intWithSep = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return decPart != null ? `${intWithSep}.${decPart}` : intWithSep;
  })();

  const finalIntPart = Math.floor(target).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const finalStr = `${prefix ?? ""}${finalIntPart}${decimalPlaces ? "." + "0".repeat(decimalPlaces) : ""}${suffix ?? ""}`;
  const AVG_CHAR = 0.62;
  const SAFE = 0.80;
  const maxByWord = (width * SAFE) / (Math.max(1, finalStr.length) * AVG_CHAR);
  const heroFontSize = Math.round(Math.min(typeBase * 0.26, maxByWord));

  const labelEnter = useFadeRise(0.00, 0.40, 12);
  const counterSpring = useSpringIn(0.10, 0.50);
  const settleZoom = useSettleZoom(startSec + dur, 1.5, 1.02);
  const chipEnter = useFadeRise(startSec + dur - 0.15, 0.4, 14);

  const direction = delta_direction ?? "up";
  const isUp = direction === "up";
  const arrow = isUp ? "▲" : "▼";
  const chipColor = isUp ? "#CFFF05" : "#B5BFC2";
  const chipTextColor = isUp ? "#0F121A" : "#E9ECED";
  const chipBg = isUp ? "#CFFF05" : "#1E2434";

  return (
    <AbsoluteFill>
      <LightGridBg />
      <AbsoluteFill style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        textAlign: "center",
        padding: width * 0.04,
      }}>
        {pre_label && (
          <div style={{
            fontFamily,
            fontWeight: 700,
            fontSize: Math.round(typeBase * 0.034),
            color: "#B5BFC2",
            textTransform: "uppercase",
            letterSpacing: "0.12em",
            marginBottom: Math.round(typeBase * 0.025),
            opacity: labelEnter.opacity,
            transform: `translateY(${labelEnter.ty}px)`,
          }}>
            {pre_label}
          </div>
        )}
        <div style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "center",
          gap: Math.round(typeBase * 0.005),
          opacity: counterSpring,
          transform: `scale(${(0.85 + 0.15 * counterSpring) * settleZoom})`,
        }}>
          {prefix && (
            <span style={{
              fontFamily,
              fontWeight: 700,
              fontSize: Math.round(heroFontSize * 0.40),
              color: "#CFFF05",
              lineHeight: 1.0,
              alignSelf: "flex-start",
              marginTop: Math.round(heroFontSize * 0.12),
            }}>
              {prefix}
            </span>
          )}
          <span style={{
            fontFamily,
            fontWeight: 700,
            fontSize: heroFontSize,
            color: "#E9ECED",
            lineHeight: 0.85,
            letterSpacing: "-0.02em",
            fontVariantNumeric: "tabular-nums",
          }}>
            {formattedHuman}
          </span>
          {suffix && (
            <span style={{
              fontFamily,
              fontWeight: 700,
              fontSize: Math.round(heroFontSize * 0.36),
              color: "#CFFF05",
              lineHeight: 1.0,
              alignSelf: "flex-end",
              marginBottom: Math.round(heroFontSize * 0.10),
            }}>
              {suffix}
            </span>
          )}
        </div>
        <div style={{
          display: "flex",
          alignItems: "center",
          gap: Math.round(typeBase * 0.012),
          marginTop: Math.round(typeBase * 0.045),
          padding: `${Math.round(typeBase * 0.014)}px ${Math.round(typeBase * 0.024)}px`,
          borderRadius: Math.round(typeBase * 0.06),
          backgroundColor: chipBg,
          opacity: chipEnter.opacity,
          transform: `translateY(${chipEnter.ty}px)`,
        }}>
          <span style={{
            fontFamily, fontWeight: 800, fontSize: Math.round(typeBase * 0.026),
            color: isUp ? chipTextColor : chipColor,
          }}>
            {arrow}
          </span>
          <span style={{
            fontFamily, fontWeight: 800, fontSize: Math.round(typeBase * 0.026),
            color: isUp ? chipTextColor : "#E9ECED",
          }}>
            {delta_value}
          </span>
          {delta_label && (
            <span style={{
              fontFamily, fontWeight: 600, fontSize: Math.round(typeBase * 0.022),
              color: isUp ? "#3A4212" : "#8A94A6",
            }}>
              {delta_label}
            </span>
          )}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
