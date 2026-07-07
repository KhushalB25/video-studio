import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";

/**
 * FX LAYER — optional per-beat visual treatment, applied ON TOP of whatever
 * `kind` the beat already renders. Ported from reactvideoeditor/remotion-templates'
 * Background (9) + Cinematic (9) + Transition (9) categories, which are NOT
 * content-driven "kinds" (no title/data/image props) — they're pure visual
 * treatments meant to sit over existing content.
 *
 * Usage: any beat can carry an optional `fx: FxName` field alongside its
 * normal `kind`. EditedVideo.tsx renders `<FxLayer fx={b.fx} />` inside the
 * beat's <Sequence>, layered above the kind's own content. Ambient/background
 * effects (bokeh, starfield, matrix-rain, etc.) render at low opacity behind
 * text is NOT possible here since they're layered above — they're designed
 * to read as a subtle wash, not obscure content, so opacity is kept low.
 * "Transition"-category effects (blinds, iris, clock-wipe, etc.) are
 * reinterpreted as an ENTRANCE wipe over the first ~0.5s of the beat, since
 * we don't have a two-scene crossfade model — they wipe the beat's own
 * content into view rather than dissolving between two different beats.
 *
 * Each branch below is deliberately self-contained (no shared sub-components)
 * so effects can be added/removed independently without touching others.
 */
export type FxName =
  // Background (ambient, low-opacity, loops for the beat's duration)
  | "bokeh_circles"
  | "geometric_patterns"
  | "gradient_shift"
  | "grid_pulse"
  | "liquid_wave"
  | "matrix_rain"
  | "noise_grain"
  | "pixel_reveal"
  | "starfield"
  // Cinematic (light/camera treatments, full-beat duration)
  | "camera_shake"
  | "film_burn"
  | "ken_burns"
  | "letterbox_reveal"
  | "parallax_pan"
  | "spotlight_reveal"
  | "vignette_pulse"
  | "whip_pan"
  | "zoom_pulse"
  // Transition-style (entrance wipe, first ~0.5s of the beat only)
  | "blinds_in"
  | "clock_wipe_in"
  | "cross_dissolve_in"
  | "fade_through_black_in"
  | "iris_in"
  | "morph_in"
  | "push_in"
  | "slide_wipe_in"
  | "zoom_through_in";

export type FxLayerProps = {
  fx?: FxName;
};

const LIME = "#CFFF05";

export const FxLayer: React.FC<FxLayerProps> = ({ fx }) => {
  const frame = useCurrentFrame();
  const { fps, width, height, durationInFrames } = useVideoConfig();
  const t = frame / fps;

  if (!fx) return null;

  // ── Background (ambient) ────────────────────────────────────────────────
  if (fx === "bokeh_circles") {
    const circles = Array.from({ length: 10 }, (_, i) => {
      const seed = i * 137.5;
      const cx = ((Math.sin(seed) * 0.5 + 0.5) * width);
      const cy = ((Math.cos(seed * 1.3) * 0.5 + 0.5) * height);
      const drift = Math.sin(t * 0.3 + seed) * 30;
      const r = 40 + (i % 4) * 25;
      return (
        <div key={i} style={{
          position: "absolute",
          left: cx - r, top: cy - r + drift,
          width: r * 2, height: r * 2, borderRadius: "50%",
          background: `radial-gradient(circle, rgba(207,255,5,0.10) 0%, transparent 70%)`,
          filter: "blur(8px)",
        }} />
      );
    });
    return <AbsoluteFill style={{ pointerEvents: "none", overflow: "hidden" }}>{circles}</AbsoluteFill>;
  }

  if (fx === "geometric_patterns") {
    const shapes = Array.from({ length: 4 }, (_, i) => {
      const rot = (t * (8 + i * 3)) % 360;
      const scale = 1 + Math.sin(t * 0.4 + i) * 0.08;
      const size = 120 + i * 60;
      return (
        <div key={i} style={{
          position: "absolute",
          left: "50%", top: "50%",
          width: size, height: size,
          marginLeft: -size / 2, marginTop: -size / 2,
          border: `1px solid rgba(207,255,5,${0.08 - i * 0.015})`,
          borderRadius: i % 2 === 0 ? "50%" : 8,
          transform: `rotate(${rot}deg) scale(${scale})`,
        }} />
      );
    });
    return <AbsoluteFill style={{ pointerEvents: "none" }}>{shapes}</AbsoluteFill>;
  }

  if (fx === "gradient_shift") {
    const hue = (t * 6) % 30;
    return (
      <AbsoluteFill style={{
        pointerEvents: "none",
        background: `linear-gradient(135deg, rgba(15,18,26,0) 0%, rgba(207,255,${5 + hue},0.06) 100%)`,
      }} />
    );
  }

  if (fx === "grid_pulse") {
    const cols = 10, rows = 6;
    const cellW = width / cols, cellH = height / rows;
    const dots = [];
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const dist = Math.hypot(c - cols / 2, r - rows / 2);
        const wave = Math.sin(t * 1.5 - dist * 0.5);
        const op = Math.max(0, 0.10 * wave);
        dots.push(
          <div key={`${r}-${c}`} style={{
            position: "absolute",
            left: c * cellW + cellW / 2 - 2,
            top: r * cellH + cellH / 2 - 2,
            width: 4, height: 4, borderRadius: "50%",
            backgroundColor: LIME, opacity: op,
          }} />
        );
      }
    }
    return <AbsoluteFill style={{ pointerEvents: "none" }}>{dots}</AbsoluteFill>;
  }

  if (fx === "liquid_wave") {
    const amp = 14, freq = 0.02;
    const yOff = height * 0.92;
    const points = Array.from({ length: 20 }, (_, i) => {
      const x = (width / 19) * i;
      const y = yOff + Math.sin(x * freq + t * 1.2) * amp;
      return `${x},${y}`;
    }).join(" L ");
    return (
      <AbsoluteFill style={{ pointerEvents: "none" }}>
        <svg width={width} height={height} style={{ position: "absolute" }}>
          <path
            d={`M 0,${height} L ${points} L ${width},${height} Z`}
            fill="rgba(207,255,5,0.05)"
          />
        </svg>
      </AbsoluteFill>
    );
  }

  if (fx === "matrix_rain") {
    const cols = 16;
    const colW = width / cols;
    const chars = "01";
    const streams = Array.from({ length: cols }, (_, c) => {
      const speed = 80 + (c % 5) * 20;
      const yOff = ((t * speed + c * 137) % (height + 200)) - 200;
      const ch = chars[(c + Math.floor(t * 4)) % chars.length];
      return (
        <div key={c} style={{
          position: "absolute", left: c * colW, top: yOff,
          fontFamily: "monospace", fontSize: 16,
          color: "rgba(207,255,5,0.12)",
        }}>{ch}</div>
      );
    });
    return <AbsoluteFill style={{ pointerEvents: "none", overflow: "hidden" }}>{streams}</AbsoluteFill>;
  }

  if (fx === "noise_grain") {
    // Deterministic pseudo-grain via CSS repeating-gradient, animated offset —
    // avoids per-frame canvas noise generation cost.
    const offset = (frame * 3) % 8;
    return (
      <AbsoluteFill style={{
        pointerEvents: "none",
        opacity: 0.03,
        backgroundImage:
          "repeating-linear-gradient(0deg, #fff 0px, transparent 1px, transparent 2px), " +
          "repeating-linear-gradient(90deg, #fff 0px, transparent 1px, transparent 2px)",
        backgroundPosition: `${offset}px ${offset}px`,
        mixBlendMode: "overlay",
      }} />
    );
  }

  if (fx === "pixel_reveal") {
    const prog = Math.min(1, t / 0.6);
    const grid = 12;
    const cellW = width / grid, cellH = height / grid;
    if (prog >= 1) return null;
    const cells = [];
    for (let r = 0; r < grid; r++) {
      for (let c = 0; c < grid; c++) {
        const cellSeed = (r * grid + c) / (grid * grid);
        const visible = cellSeed < prog;
        if (!visible) {
          cells.push(
            <div key={`${r}-${c}`} style={{
              position: "absolute",
              left: c * cellW, top: r * cellH,
              width: cellW + 1, height: cellH + 1,
              backgroundColor: "#0F121A",
            }} />
          );
        }
      }
    }
    return <AbsoluteFill style={{ pointerEvents: "none" }}>{cells}</AbsoluteFill>;
  }

  if (fx === "starfield") {
    const stars = Array.from({ length: 40 }, (_, i) => {
      const seed = i * 53.7;
      const baseX = (Math.sin(seed) * 0.5 + 0.5) * width;
      const baseY = (Math.cos(seed * 1.7) * 0.5 + 0.5) * height;
      const speed = 0.3 + (i % 5) * 0.15;
      const cx = width / 2, cy = height / 2;
      const dx = baseX - cx, dy = baseY - cy;
      const k = 1 + ((t * speed) % 3);
      const x = cx + dx * k, y = cy + dy * k;
      const op = Math.max(0, 1 - (k - 1) / 2) * 0.35;
      if (x < 0 || x > width || y < 0 || y > height) return null;
      return (
        <div key={i} style={{
          position: "absolute", left: x, top: y,
          width: 2, height: 2, borderRadius: "50%",
          backgroundColor: "#FFFFFF", opacity: op,
        }} />
      );
    });
    return <AbsoluteFill style={{ pointerEvents: "none", overflow: "hidden" }}>{stars}</AbsoluteFill>;
  }

  // ── Cinematic ────────────────────────────────────────────────────────────
  if (fx === "camera_shake") {
    // Decaying shake — strongest at beat start, settles by ~0.5s.
    const decay = Math.max(0, 1 - t / 0.5);
    const sx = Math.sin(frame * 2.3) * 6 * decay;
    const sy = Math.cos(frame * 3.1) * 6 * decay;
    return (
      <AbsoluteFill style={{
        pointerEvents: "none",
        transform: `translate(${sx}px, ${sy}px)`,
      }} />
    );
  }

  if (fx === "film_burn") {
    const cx = width * (0.3 + Math.sin(t * 0.5) * 0.2);
    const cy = height * 0.4;
    return (
      <AbsoluteFill style={{
        pointerEvents: "none",
        background: `radial-gradient(circle at ${cx}px ${cy}px, rgba(255,180,80,0.10) 0%, transparent 45%)`,
        mixBlendMode: "screen",
      }} />
    );
  }

  if (fx === "ken_burns") {
    const prog = Math.min(1, t / (durationInFrames / fps));
    const scale = 1.0 + prog * 0.06;
    return (
      <AbsoluteFill style={{ pointerEvents: "none", transform: `scale(${scale})`, transformOrigin: "center" }} />
    );
  }

  if (fx === "letterbox_reveal") {
    const prog = Math.min(1, t / 0.6);
    const barH = (height * 0.10) * (1 - prog);
    return (
      <AbsoluteFill style={{ pointerEvents: "none" }}>
        <div style={{ position: "absolute", top: 0, left: 0, right: 0, height: barH, backgroundColor: "#000" }} />
        <div style={{ position: "absolute", bottom: 0, left: 0, right: 0, height: barH, backgroundColor: "#000" }} />
      </AbsoluteFill>
    );
  }

  if (fx === "parallax_pan") {
    const prog = t / (durationInFrames / fps);
    const dx = interpolateLinear(prog, 0, 1, -width * 0.015, width * 0.015);
    return (
      <AbsoluteFill style={{ pointerEvents: "none", transform: `translateX(${dx}px)` }} />
    );
  }

  if (fx === "spotlight_reveal") {
    const prog = Math.min(1, t / 0.6);
    if (prog >= 1) return null;
    const r = Math.max(width, height) * (0.15 + prog * 1.0);
    return (
      <AbsoluteFill style={{
        pointerEvents: "none",
        background: "#0F121A",
        WebkitMaskImage: `radial-gradient(circle ${r}px at 50% 50%, transparent 99%, black 100%)`,
        maskImage: `radial-gradient(circle ${r}px at 50% 50%, transparent 99%, black 100%)`,
      }} />
    );
  }

  if (fx === "vignette_pulse") {
    const pulse = 0.35 + 0.10 * Math.sin(t * 1.2);
    return (
      <AbsoluteFill style={{
        pointerEvents: "none",
        boxShadow: `inset 0 0 ${Math.round(width * 0.15)}px ${Math.round(width * 0.04)}px rgba(0,0,0,${pulse})`,
      }} />
    );
  }

  if (fx === "whip_pan") {
    const prog = Math.min(1, t / 0.35);
    if (prog >= 1) return null;
    const blur = (1 - prog) * 18;
    const sx = 1 + (1 - prog) * 0.15;
    return (
      <AbsoluteFill style={{
        pointerEvents: "none",
        backdropFilter: `blur(${blur}px)`,
        transform: `scaleX(${sx})`,
      }} />
    );
  }

  if (fx === "zoom_pulse") {
    const pulse = 1 + 0.015 * Math.sin(t * 2.0);
    return (
      <AbsoluteFill style={{ pointerEvents: "none", transform: `scale(${pulse})`, transformOrigin: "center" }} />
    );
  }

  // ── Transition-style entrance wipes (first ~0.5s of the beat) ───────────
  const WIPE_DUR = 0.5;
  if (t >= WIPE_DUR && fx.endsWith("_in")) return null;
  const wipeK = Math.min(1, t / WIPE_DUR);

  if (fx === "blinds_in") {
    const slats = 8;
    const slatH = height / slats;
    const bars = Array.from({ length: slats }, (_, i) => {
      const delay = i * 0.03;
      const localK = Math.max(0, Math.min(1, (wipeK - delay) / (1 - delay || 1)));
      return (
        <div key={i} style={{
          position: "absolute",
          left: 0, right: 0, top: i * slatH,
          height: slatH,
          backgroundColor: "#0F121A",
          transform: `scaleY(${1 - localK})`,
          transformOrigin: "top",
        }} />
      );
    });
    return <AbsoluteFill style={{ pointerEvents: "none" }}>{bars}</AbsoluteFill>;
  }

  if (fx === "clock_wipe_in") {
    const angle = wipeK * 360;
    const cx = width / 2, cy = height / 2;
    const r = Math.hypot(cx, cy);
    const rad = (angle - 90) * (Math.PI / 180);
    const x2 = cx + r * Math.cos(rad), y2 = cy + r * Math.sin(rad);
    const largeArc = angle > 180 ? 1 : 0;
    const path = `M ${cx} ${cy} L ${cx} ${cy - r} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2} Z`;
    return (
      <AbsoluteFill style={{ pointerEvents: "none" }}>
        <svg width={width} height={height} style={{ position: "absolute" }}>
          <path d={path} fill="#0F121A" />
        </svg>
      </AbsoluteFill>
    );
  }

  if (fx === "cross_dissolve_in") {
    return <AbsoluteFill style={{ pointerEvents: "none", backgroundColor: "#0F121A", opacity: 1 - wipeK }} />;
  }

  if (fx === "fade_through_black_in") {
    const dip = wipeK < 0.5 ? 1 - wipeK * 2 : (wipeK - 0.5) * 2;
    const op = wipeK < 0.5 ? 1 : 1 - (wipeK - 0.5) * 2;
    return <AbsoluteFill style={{ pointerEvents: "none", backgroundColor: "#000", opacity: Math.max(0, op) }} />;
  }

  if (fx === "iris_in") {
    if (wipeK >= 1) return null;
    const r = Math.max(width, height) * 0.75 * wipeK;
    return (
      <AbsoluteFill style={{
        pointerEvents: "none",
        background: "#0F121A",
        WebkitMaskImage: `radial-gradient(circle ${r}px at 50% 50%, transparent 99%, black 100%)`,
        maskImage: `radial-gradient(circle ${r}px at 50% 50%, transparent 99%, black 100%)`,
      }} />
    );
  }

  if (fx === "morph_in") {
    const scale = interpolateLinear(wipeK, 0, 1, 0.85, 1);
    return (
      <AbsoluteFill style={{
        pointerEvents: "none",
        backgroundColor: "#0F121A",
        opacity: 1 - wipeK,
        transform: `scale(${scale})`,
      }} />
    );
  }

  if (fx === "push_in") {
    const tx = interpolateLinear(wipeK, 0, 1, width, 0);
    return (
      <AbsoluteFill style={{
        pointerEvents: "none",
        backgroundColor: "#0F121A",
        transform: `translateX(${tx}px)`,
      }} />
    );
  }

  if (fx === "slide_wipe_in") {
    const ty = interpolateLinear(wipeK, 0, 1, 0, -height);
    return (
      <AbsoluteFill style={{
        pointerEvents: "none",
        backgroundColor: "#0F121A",
        transform: `translateY(${ty}px)`,
      }} />
    );
  }

  if (fx === "zoom_through_in") {
    if (wipeK >= 1) return null;
    const scale = interpolateLinear(wipeK, 0, 1, 3, 1);
    return (
      <AbsoluteFill style={{
        pointerEvents: "none",
        backgroundColor: "#0F121A",
        opacity: 1 - wipeK,
        transform: `scale(${scale})`,
      }} />
    );
  }

  return null;
};

function interpolateLinear(t: number, t0: number, t1: number, v0: number, v1: number): number {
  const k = Math.max(0, Math.min(1, (t - t0) / (t1 - t0 || 1)));
  return v0 + (v1 - v0) * k;
}
