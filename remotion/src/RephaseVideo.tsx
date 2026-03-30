import React from "react";
import {
  AbsoluteFill,
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  interpolateColors,
  spring,
  Easing,
} from "remotion";

// ── Brand colors ──────────────────────────────────────────────────────────────
const BG     = "#1a1a1a";
const GREEN  = "#34c759";
const RED    = "#FF3B30";
const ORANGE = "#FF9500";
const WHITE  = "#ffffff";
const DIM    = "#888888";

// ── Real measurement data ─────────────────────────────────────────────────────
const ORIG_HZ     = 438.7284;
const ORIG_CENTS  = 25.177;
const CONV_HZ     = 431.8482;
const CONV_CENTS  = -0.608;
const SHIFT_CENTS = -26.75;
const SHA256      = "367fe72a...";
const TIMESTAMP   = "29 marzo 2026 · 21:03 CET";

// ── Frame boundaries (30 fps, 1800 frames = 60 s) ────────────────────────────
const S1_END = 180;   // 0:00–0:06  Views hook
const S2_END = 360;   // 0:06–0:12  Question pulse
const S3_END = 750;   // 0:12–0:25  FFT animated
const S4_END = 1140;  // 0:25–0:38  438.7 blink + badge
const S5_END = 1500;  // 0:38–0:50  Shift animation
const S6_END = 1710;  // 0:50–0:57  431.8 certified
// 1710–1800              0:57–1:00  Logo CTA

const FADE = 8;

// ── Custom hook: pixel scale relative to min(width,height) = 1080 ─────────────
function useScale() {
  const { width, height } = useVideoConfig();
  const ref = Math.min(width, height);
  return (n: number) => Math.round((n * ref) / 1080);
}

// ── Fade helper ───────────────────────────────────────────────────────────────
function fade(localFrame: number, duration: number, f = FADE): number {
  const i = interpolate(localFrame, [0, f], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const o = interpolate(localFrame, [duration - f, duration], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return Math.min(i, o);
}

// ── Gaussian FFT spectrum ─────────────────────────────────────────────────────
function buildSpectrum(peakHz: number, freqs: number[]): number[] {
  const rng = (i: number) => {
    const x = Math.sin(42 * 127.1 + i * 311.7) * 43758.5453;
    return x - Math.floor(x);
  };
  return freqs.map((f, i) => {
    const main  = Math.exp(-((f - peakHz) ** 2) / (2 * 7 ** 2));
    const harm  = 0.15 * Math.exp(-((f - peakHz * 2) ** 2) / (2 * 9 ** 2));
    const sub   = 0.10 * Math.exp(-((f - peakHz * 0.5) ** 2) / (2 * 11 ** 2));
    const noise = (rng(i) - 0.5) * 0.025;
    return Math.max(0, main + harm + sub + noise);
  });
}

// ── SVG FFT chart ─────────────────────────────────────────────────────────────
const FFTChart: React.FC<{
  peakHz: number;
  peakColor: string;
  progress: number;
  cw: number;
  ch: number;
  counterHz?: number;
  showCounter?: boolean;
}> = ({ peakHz, peakColor, progress, cw, ch, counterHz, showCounter }) => {
  const pad = { top: 36, right: 28, bottom: 60, left: 64 };
  const W = cw - pad.left - pad.right;
  const H = ch - pad.top - pad.bottom;

  const fMin = 400, fMax = 480, N = 280;
  const freqs = Array.from({ length: N }, (_, i) => fMin + (i / (N - 1)) * (fMax - fMin));
  const spectrum = buildSpectrum(peakHz, freqs);
  const revN = Math.floor(progress * N);

  const toX = (f: number) => pad.left + ((f - fMin) / (fMax - fMin)) * W;
  const toY = (v: number) => pad.top + (1 - v) * H;

  const pts = freqs.slice(0, revN).map((f, i) => `${toX(f)},${toY(spectrum[i])}`).join(" ");
  const peakX = toX(peakHz);
  const peakY = toY(1.0);

  const xTicks = [410, 420, 432, 440, 450, 460, 470];

  return (
    <svg width={cw} height={ch} style={{ display: "block" }}>
      {/* BG */}
      <rect width={cw} height={ch} fill="#0f0f0f" rx={16} />

      {/* Grid */}
      {[0.25, 0.5, 0.75].map((v) => (
        <line key={v} x1={pad.left} y1={toY(v)} x2={pad.left + W} y2={toY(v)}
          stroke="rgba(255,255,255,0.05)" strokeWidth={1} />
      ))}

      {/* 432 Hz reference */}
      <line x1={toX(432)} y1={pad.top} x2={toX(432)} y2={pad.top + H}
        stroke={GREEN} strokeWidth={2} strokeDasharray="8,5" opacity={0.75} />
      <text x={toX(432)} y={pad.top - 8} textAnchor="middle"
        fill={GREEN} fontSize={12} fontWeight="700" fontFamily="monospace">
        432 Hz
      </text>

      {/* 440 Hz reference */}
      <line x1={toX(440)} y1={pad.top} x2={toX(440)} y2={pad.top + H}
        stroke={ORANGE} strokeWidth={2} strokeDasharray="8,5" opacity={0.65} />
      <text x={toX(440)} y={pad.top - 8} textAnchor="middle"
        fill={ORANGE} fontSize={12} fontWeight="600" fontFamily="monospace">
        440 Hz
      </text>

      {/* Spectrum */}
      {pts && <polyline points={pts} fill="none" stroke={peakColor} strokeWidth={3} strokeLinejoin="round" />}

      {/* Peak marker */}
      {progress > 0.65 && (
        <>
          <line x1={peakX} y1={peakY} x2={peakX} y2={peakY - 36}
            stroke={peakColor} strokeWidth={2} />
          <rect x={peakX - 50} y={peakY - 66} width={100} height={30} rx={8} fill={peakColor} />
          <text x={peakX} y={peakY - 46} textAnchor="middle"
            fill="#fff" fontSize={15} fontWeight="800" fontFamily="monospace">
            {peakHz.toFixed(1)} Hz
          </text>
        </>
      )}

      {/* Axes */}
      <line x1={pad.left} y1={pad.top + H} x2={pad.left + W} y2={pad.top + H}
        stroke={DIM} strokeWidth={1} />
      <line x1={pad.left} y1={pad.top} x2={pad.left} y2={pad.top + H}
        stroke={DIM} strokeWidth={1} />

      {xTicks.map((t) => (
        <g key={t}>
          <line x1={toX(t)} y1={pad.top + H} x2={toX(t)} y2={pad.top + H + 6}
            stroke={DIM} strokeWidth={1} />
          <text x={toX(t)} y={pad.top + H + 20} textAnchor="middle"
            fill={t === 432 ? GREEN : t === 440 ? ORANGE : DIM}
            fontSize={t === 432 || t === 440 ? 13 : 11}
            fontWeight={t === 432 || t === 440 ? "700" : "400"}
            fontFamily="monospace">
            {t}
          </text>
        </g>
      ))}

      {/* Y axis label */}
      <text x={16} y={pad.top + H / 2} textAnchor="middle" fill={DIM}
        fontSize={11} fontFamily="sans-serif"
        transform={`rotate(-90, 16, ${pad.top + H / 2})`}>
        Ampiezza norm.
      </text>

      {/* X axis label */}
      <text x={pad.left + W / 2} y={ch - 6} textAnchor="middle"
        fill={DIM} fontSize={11} fontFamily="sans-serif">
        Frequenza (Hz)
      </text>

      {/* Hz counter bottom-left */}
      {showCounter && counterHz !== undefined && (
        <text x={pad.left + 10} y={pad.top + H - 10}
          fill={peakColor} fontSize={22} fontWeight="900" fontFamily="monospace">
          {counterHz.toFixed(1)} Hz
        </text>
      )}
    </svg>
  );
};

// ── Scene 1: Views hook (0–179) ───────────────────────────────────────────────
const Scene1: React.FC<{ frame: number }> = ({ frame }) => {
  const { fps } = useVideoConfig();
  const px = useScale();
  const op = fade(frame, S1_END);

  const badge  = spring({ frame, fps, from: 0, to: 1, config: { damping: 22 } });
  const title  = spring({ frame: frame - 16, fps, from: 0, to: 1, config: { damping: 22 } });
  const sub    = spring({ frame: frame - 36, fps, from: 0, to: 1, config: { damping: 22 } });

  return (
    <AbsoluteFill style={{
      background: BG, opacity: op,
      alignItems: "center", justifyContent: "center",
      flexDirection: "column", padding: `0 ${px(72)}px`,
    }}>
      {/* Views badge */}
      <div style={{
        opacity: badge,
        background: "rgba(255,255,255,0.07)",
        border: "1px solid rgba(255,255,255,0.14)",
        borderRadius: px(40),
        padding: `${px(14)}px ${px(40)}px`,
        marginBottom: px(44),
      }}>
        <span style={{ color: WHITE, fontSize: px(30), fontWeight: 800, fontFamily: "sans-serif" }}>
          ▶ &nbsp;58.000.000 di visualizzazioni
        </span>
      </div>

      {/* Title */}
      <div style={{
        opacity: title,
        color: WHITE, fontSize: px(50), fontWeight: 900,
        fontFamily: "sans-serif", textAlign: "center", lineHeight: 1.2,
        marginBottom: px(32),
      }}>
        "432 Hz - Deep Healing Music<br />
        for The Body &amp; Soul"
      </div>

      {/* Sub */}
      <div style={{
        opacity: Math.max(0, sub),
        color: DIM, fontSize: px(28),
        fontFamily: "sans-serif", textAlign: "center",
      }}>
        Il brano 432 Hz più famoso su YouTube
      </div>

      <div style={{
        position: "absolute", bottom: px(80),
        color: DIM, fontSize: px(20), fontFamily: "monospace", opacity: 0.5,
      }}>
        rephase.app
      </div>
    </AbsoluteFill>
  );
};

// ── Scene 2: Question pulse (180–359) ────────────────────────────────────────
const Scene2: React.FC<{ frame: number }> = ({ frame }) => {
  const { fps } = useVideoConfig();
  const px = useScale();
  const lf = frame - S1_END;
  const op = fade(lf, S2_END - S1_END);

  const entry = spring({ frame: lf, fps, from: 0.6, to: 1, config: { mass: 0.8, damping: 14, stiffness: 200 } });
  const pulse = 1 + 0.045 * Math.sin(lf * Math.PI / 24);
  const sub   = spring({ frame: lf - 28, fps, from: 0, to: 1, config: { damping: 20 } });

  return (
    <AbsoluteFill style={{
      background: BG, opacity: op,
      alignItems: "center", justifyContent: "center",
      flexDirection: "column", gap: px(20),
    }}>
      <div style={{ transform: `scale(${entry * pulse})`, textAlign: "center" }}>
        <div style={{ color: WHITE, fontSize: px(88), fontWeight: 900, fontFamily: "sans-serif", lineHeight: 1.1 }}>
          È davvero
        </div>
        <div style={{ color: GREEN, fontSize: px(108), fontWeight: 900, fontFamily: "sans-serif", lineHeight: 1.1 }}>
          a 432 Hz?
        </div>
      </div>

      <div style={{
        opacity: Math.max(0, sub),
        color: DIM, fontSize: px(24),
        fontFamily: "sans-serif", textAlign: "center",
      }}>
        Analizziamo con Rephase — test FFT su brano reale
      </div>
    </AbsoluteFill>
  );
};

// ── Scene 3: FFT animated (360–749) ──────────────────────────────────────────
const Scene3: React.FC<{ frame: number }> = ({ frame }) => {
  const { fps, width } = useVideoConfig();
  const px = useScale();
  const lf = frame - S2_END;
  const dur = S3_END - S2_END; // 390
  const op = fade(lf, dur);

  const headerOp = spring({ frame: lf, fps, from: 0, to: 1, config: { damping: 20 } });

  const progress = interpolate(lf, [0, dur * 0.72], [0, 1], {
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });

  const counterHz = interpolate(lf, [0, dur * 0.68], [400, ORIG_HZ], {
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.quad),
  });
  const locked = lf >= dur * 0.68;

  const cw = width - px(80);
  const ch = px(500);

  const statusOp = spring({ frame: lf - 12, fps, from: 0, to: 1, config: { damping: 18 } });
  const alertOp  = spring({ frame: Math.max(0, lf - dur * 0.72), fps, from: 0, to: 1, config: { damping: 16 } });

  return (
    <AbsoluteFill style={{
      background: BG, opacity: op,
      flexDirection: "column",
      padding: `${px(44)}px ${px(40)}px`,
    }}>
      {/* Header */}
      <div style={{ opacity: headerOp, marginBottom: px(18) }}>
        <div style={{
          color: locked ? RED : GREEN,
          fontSize: px(20), fontFamily: "sans-serif", fontWeight: 700,
          marginBottom: px(4),
        }}>
          {locked ? "⚡ Frequenza rilevata" : "⏳ Analisi FFT in corso..."}
        </div>
        <div style={{ color: WHITE, fontSize: px(21), fontFamily: "sans-serif" }}>
          "432 Hz - Deep Healing Music for The Body & Soul"
        </div>
      </div>

      {/* Chart */}
      <div style={{ opacity: statusOp }}>
        <FFTChart
          peakHz={ORIG_HZ}
          peakColor={locked ? RED : "#aaaaaa"}
          progress={progress}
          cw={cw}
          ch={ch}
          showCounter={true}
          counterHz={locked ? ORIG_HZ : counterHz}
        />
      </div>

      {/* Status pills */}
      <div style={{
        opacity: alertOp,
        marginTop: px(22),
        display: "flex", gap: px(16), flexWrap: "wrap",
      }}>
        <div style={{
          background: "rgba(255,59,48,0.1)", border: `1px solid ${RED}`,
          borderRadius: px(12), padding: `${px(12)}px ${px(22)}px`,
        }}>
          <span style={{ color: DIM, fontSize: px(16), fontFamily: "sans-serif" }}>Picco: </span>
          <span style={{ color: RED, fontSize: px(16), fontWeight: 700, fontFamily: "monospace" }}>
            {ORIG_HZ.toFixed(4)} Hz
          </span>
        </div>
        <div style={{
          background: "rgba(255,59,48,0.1)", border: `1px solid ${RED}`,
          borderRadius: px(12), padding: `${px(12)}px ${px(22)}px`,
        }}>
          <span style={{ color: DIM, fontSize: px(16), fontFamily: "sans-serif" }}>Δ da 432 Hz: </span>
          <span style={{ color: RED, fontSize: px(16), fontWeight: 700, fontFamily: "monospace" }}>
            +{ORIG_CENTS.toFixed(3)} cent
          </span>
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ── Scene 4: 438.7 blink + NON CERTIFICATO (750–1139) ────────────────────────
const Scene4: React.FC<{ frame: number }> = ({ frame }) => {
  const { fps } = useVideoConfig();
  const px = useScale();
  const lf = frame - S3_END;
  const dur = S4_END - S3_END; // 390
  const op = fade(lf, dur);

  // 3 blink cycles via sine — period ≈ 130 frames / cycle
  const blink = 0.5 + 0.5 * Math.abs(Math.sin((lf * Math.PI) / (dur / 3)));

  const sub   = spring({ frame: lf - 18, fps, from: 0, to: 1, config: { damping: 20 } });
  const badge = spring({ frame: Math.max(0, lf - 60), fps, from: 0, to: 1, config: { mass: 0.7, damping: 12, stiffness: 200 } });
  const hash  = spring({ frame: Math.max(0, lf - 90), fps, from: 0, to: 1, config: { damping: 20 } });

  return (
    <AbsoluteFill style={{
      background: BG, opacity: op,
      alignItems: "center", justifyContent: "center",
      flexDirection: "column", padding: `0 ${px(60)}px`,
    }}>
      {/* Big blinking Hz */}
      <div style={{ opacity: blink, textAlign: "center", marginBottom: px(8) }}>
        <div style={{
          color: RED, fontSize: px(164), fontWeight: 900,
          fontFamily: "monospace", lineHeight: 1, letterSpacing: -2,
        }}>
          438.7
        </div>
        <div style={{ color: RED, fontSize: px(64), fontWeight: 700, fontFamily: "monospace" }}>
          Hz
        </div>
      </div>

      {/* Not 432 */}
      <div style={{ opacity: Math.max(0, sub), textAlign: "center", marginBottom: px(36) }}>
        <div style={{ color: WHITE, fontSize: px(38), fontFamily: "sans-serif", fontWeight: 700, marginBottom: px(8) }}>
          Non è 432 Hz
        </div>
        <div style={{ color: DIM, fontSize: px(26), fontFamily: "sans-serif" }}>
          +{ORIG_CENTS.toFixed(1)} cent di scostamento
        </div>
      </div>

      {/* Badge NON CERTIFICATO */}
      <div style={{
        transform: `scale(${Math.max(0, badge)})`, transformOrigin: "center",
        background: "rgba(255,59,48,0.14)", border: `2px solid ${RED}`,
        borderRadius: px(22), padding: `${px(20)}px ${px(48)}px`,
        textAlign: "center",
      }}>
        <div style={{ color: RED, fontSize: px(34), fontWeight: 900, fontFamily: "sans-serif" }}>
          ✗ NON CERTIFICATO
        </div>
      </div>

      {/* Hash + timestamp */}
      <div style={{
        opacity: Math.max(0, hash),
        marginTop: px(28),
        color: DIM, fontSize: px(17), fontFamily: "monospace", textAlign: "center",
      }}>
        SHA256: {SHA256} &nbsp;·&nbsp; {TIMESTAMP}
      </div>
    </AbsoluteFill>
  );
};

// ── Scene 5: Shift animation (1140–1499) ─────────────────────────────────────
const Scene5: React.FC<{ frame: number }> = ({ frame }) => {
  const { fps, width } = useVideoConfig();
  const px = useScale();
  const lf = frame - S4_END;
  const dur = S5_END - S4_END; // 360
  const op = fade(lf, dur);

  const headerOp = spring({ frame: lf, fps, from: 0, to: 1, config: { damping: 20 } });

  // Shift progress: 0→1 over 65% of scene
  const shiftProg = interpolate(lf, [0, dur * 0.65], [0, 1], {
    extrapolateRight: "clamp",
    easing: Easing.inOut(Easing.quad),
  });

  const currentHz = ORIG_HZ + (CONV_HZ - ORIG_HZ) * shiftProg;
  const peakColor = interpolateColors(shiftProg, [0, 0.5, 1], [RED, ORANGE, GREEN]);
  const hzColor   = interpolateColors(shiftProg, [0, 0.5, 1], [RED, ORANGE, GREEN]);

  const badgeOp = spring({ frame: Math.max(0, lf - dur * 0.68), fps, from: 0, to: 1, config: { mass: 0.7, damping: 12, stiffness: 190 } });

  const cw = width - px(80);
  const ch = px(480);

  return (
    <AbsoluteFill style={{
      background: BG, opacity: op,
      flexDirection: "column",
      padding: `${px(44)}px ${px(40)}px`,
    }}>
      {/* Header */}
      <div style={{ opacity: headerOp, marginBottom: px(14) }}>
        <div style={{ color: GREEN, fontSize: px(22), fontFamily: "sans-serif", fontWeight: 700 }}>
          ⟵ Rephase calcola lo shift esatto: {SHIFT_CENTS} cent
        </div>
        <div style={{ color: WHITE, fontSize: px(20), fontFamily: "sans-serif", marginTop: px(4) }}>
          Conversione in tempo reale → 432 Hz
        </div>
      </div>

      {/* Hz counter */}
      <div style={{ opacity: headerOp, textAlign: "center", marginBottom: px(16) }}>
        <span style={{ color: hzColor, fontSize: px(76), fontWeight: 900, fontFamily: "monospace" }}>
          {currentHz.toFixed(1)} Hz
        </span>
      </div>

      {/* Chart — peak shifts */}
      <FFTChart
        peakHz={currentHz}
        peakColor={peakColor}
        progress={1}
        cw={cw}
        ch={ch}
      />

      {/* Completion badge */}
      <div style={{
        marginTop: px(20),
        opacity: badgeOp > 0 ? 1 : 0,
        transform: `scale(${Math.max(0.01, badgeOp)})`, transformOrigin: "center",
        background: "rgba(52,199,89,0.12)", border: `2px solid ${GREEN}`,
        borderRadius: px(18), padding: `${px(16)}px ${px(32)}px`,
        textAlign: "center",
      }}>
        <div style={{ color: GREEN, fontSize: px(28), fontWeight: 800, fontFamily: "sans-serif" }}>
          Shift applicato con precisione
        </div>
        <div style={{ color: DIM, fontSize: px(17), fontFamily: "monospace", marginTop: px(4) }}>
          {ORIG_HZ.toFixed(4)} Hz &nbsp;→&nbsp; {CONV_HZ.toFixed(4)} Hz
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ── Scene 6: Certified (1500–1709) ────────────────────────────────────────────
const Scene6: React.FC<{ frame: number }> = ({ frame }) => {
  const { fps } = useVideoConfig();
  const px = useScale();
  const lf = frame - S5_END;
  const dur = S6_END - S5_END; // 210
  const op = fade(lf, dur);

  const hzScale = spring({ frame: lf, fps, from: 0.3, to: 1, config: { mass: 0.8, damping: 13, stiffness: 180 } });
  const badge   = spring({ frame: lf - 28, fps, from: 0, to: 1, config: { mass: 0.7, damping: 12, stiffness: 200 } });
  const detOp   = spring({ frame: lf - 52, fps, from: 0, to: 1, config: { damping: 20 } });

  return (
    <AbsoluteFill style={{
      background: BG, opacity: op,
      alignItems: "center", justifyContent: "center",
      flexDirection: "column", padding: `0 ${px(60)}px`,
    }}>
      {/* Hz */}
      <div style={{ transform: `scale(${hzScale})`, textAlign: "center", marginBottom: px(24) }}>
        <div style={{ color: GREEN, fontSize: px(158), fontWeight: 900, fontFamily: "monospace", lineHeight: 1 }}>
          431.8
        </div>
        <div style={{ color: GREEN, fontSize: px(62), fontWeight: 700, fontFamily: "monospace" }}>
          Hz
        </div>
      </div>

      {/* Badge */}
      <div style={{
        transform: `scale(${Math.max(0.01, badge)})`, transformOrigin: "center",
        background: "rgba(52,199,89,0.15)", border: `2px solid ${GREEN}`,
        borderRadius: px(22), padding: `${px(20)}px ${px(52)}px`,
        textAlign: "center", marginBottom: px(30),
      }}>
        <div style={{ color: GREEN, fontSize: px(38), fontWeight: 900, fontFamily: "sans-serif" }}>
          ✓ CERTIFICATO
        </div>
      </div>

      {/* Details */}
      <div style={{ opacity: Math.max(0, detOp), textAlign: "center" }}>
        <div style={{ color: WHITE, fontSize: px(26), fontFamily: "sans-serif", marginBottom: px(8) }}>
          Scostamento da 432 Hz:&nbsp;
          <span style={{ color: GREEN, fontFamily: "monospace" }}>{CONV_CENTS} cent</span>
        </div>
        <div style={{ color: DIM, fontSize: px(18), fontFamily: "monospace" }}>
          SHA256: {SHA256} &nbsp;·&nbsp; {TIMESTAMP}
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ── Scene 7: Logo CTA + fade out (1710–1800) ──────────────────────────────────
const Scene7: React.FC<{ frame: number }> = ({ frame }) => {
  const { fps } = useVideoConfig();
  const px = useScale();
  const lf = frame - S6_END;
  const dur = 1800 - S6_END; // 90
  const op = fade(lf, dur, 10);

  const logo = spring({ frame: lf, fps, from: 0, to: 1, config: { damping: 20 } });
  const sub  = spring({ frame: lf - 14, fps, from: 0, to: 1, config: { damping: 20 } });
  const cta  = spring({ frame: lf - 26, fps, from: 0, to: 1, config: { damping: 20 } });

  return (
    <AbsoluteFill style={{
      background: BG, opacity: op,
      alignItems: "center", justifyContent: "center",
      flexDirection: "column", gap: px(20),
    }}>
      {/* Logo wordmark */}
      <div style={{ transform: `scale(${logo})`, textAlign: "center" }}>
        <div style={{ fontSize: px(108), fontWeight: 900, fontFamily: "sans-serif", letterSpacing: -2, lineHeight: 1 }}>
          <span style={{ color: WHITE }}>re</span>
          <span style={{ color: GREEN }}>phase</span>
        </div>
      </div>

      {/* URL */}
      <div style={{ opacity: Math.max(0, sub), color: DIM, fontSize: px(30), fontFamily: "monospace" }}>
        rephase.app
      </div>

      {/* CTA */}
      <div style={{ opacity: Math.max(0, cta), color: DIM, fontSize: px(24), fontFamily: "sans-serif", textAlign: "center" }}>
        Verifica gratis — nessuna carta richiesta
      </div>
    </AbsoluteFill>
  );
};

// ── Main composition ──────────────────────────────────────────────────────────
export const RephaseVideo: React.FC = () => {
  const frame = useCurrentFrame();

  return (
    <AbsoluteFill style={{ background: BG }}>
      {frame < S1_END  && <Scene1 frame={frame} />}
      {frame >= S1_END && frame < S2_END  && <Scene2 frame={frame} />}
      {frame >= S2_END && frame < S3_END  && <Scene3 frame={frame} />}
      {frame >= S3_END && frame < S4_END  && <Scene4 frame={frame} />}
      {frame >= S4_END && frame < S5_END  && <Scene5 frame={frame} />}
      {frame >= S5_END && frame < S6_END  && <Scene6 frame={frame} />}
      {frame >= S6_END                    && <Scene7 frame={frame} />}
    </AbsoluteFill>
  );
};
