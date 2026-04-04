#!/usr/bin/env python3
import os, math, wave, tempfile, subprocess
import numpy as np
from scipy.fft import rfft, rfftfreq
from scipy.signal import find_peaks

TARGET_HZ = 432.0
SR_ANALYSIS = 44100
WINDOW_SIZE = 131072
N_WINDOWS = 20
A4_MIN = 420.0
A4_MAX = 460.0
CERT_THRESHOLD_CENTS = 5.0

def _measure_a4(samples, sr):
    positions = np.linspace(WINDOW_SIZE, max(WINDOW_SIZE+1, len(samples)-WINDOW_SIZE), N_WINDOWS).astype(int)
    votes = []
    spec_acc = None
    Fm_ref = None
    spec_cnt = 0
    for pos in positions:
        chunk = samples[pos:pos+WINDOW_SIZE]
        if len(chunk) < WINDOW_SIZE: continue
        S = np.abs(rfft(chunk * np.hanning(WINDOW_SIZE)))
        F = rfftfreq(WINDOW_SIZE, 1.0/sr)
        res = F[1]-F[0]
        mask = (F>=50)&(F<=2000)
        Sm, Fm = S[mask], F[mask]
        if spec_acc is None:
            spec_acc = Sm.copy(); Fm_ref = Fm
        else:
            spec_acc = spec_acc + Sm
        spec_cnt += 1
        if Sm.max() == 0: continue
        peaks, _ = find_peaks(Sm, height=Sm.max()*0.01, distance=int(20/res))
        if len(peaks) == 0: continue
        pf = Fm[peaks]; pa = Sm[peaks]
        order = np.argsort(pa)[::-1]
        pf, pa = pf[order[:15]], pa[order[:15]]
        for freq, amp in zip(pf, pa):
            idx = int(np.argmin(np.abs(Fm-freq)))
            if 0 < idx < len(Sm)-1:
                a, b, c = Sm[idx-1], Sm[idx], Sm[idx+1]
                denom = a-2*b+c
                if denom != 0:
                    freq = Fm[idx] + 0.5*(a-c)/denom*res
            f4 = freq
            while f4 < A4_MIN: f4 *= 2
            while f4 > A4_MAX: f4 /= 2
            if A4_MIN <= f4 <= A4_MAX:
                votes.append((f4, float(amp)))
    if not votes:
        return {"success": False, "error": "Nessun picco A4 rilevato"}
    fv = np.array([v[0] for v in votes])
    av = np.array([v[1] for v in votes])
    a4 = float(np.average(fv, weights=av))
    c432 = 1200*math.log2(a4/TARGET_HZ)
    c440 = 1200*math.log2(a4/440.0)
    is_432 = abs(c432) < CERT_THRESHOLD_CENTS
    is_440 = abs(c440) < CERT_THRESHOLD_CENTS
    verdict = "432 Hz" if is_432 else ("440 Hz" if is_440 else ("vicino a 432 Hz" if abs(c432)<abs(c440) else "vicino a 440 Hz"))
    # Build averaged spectrum sampled every 2 Hz, 50-2000 Hz, normalised 0-1
    fft_freqs_out, fft_amps_out = [], []
    if spec_acc is not None and spec_cnt > 0:
        S_avg = spec_acc / spec_cnt
        target_f = np.arange(50, 2001, 2, dtype=float)
        S_samp = np.interp(target_f, Fm_ref, S_avg)
        peak_val = S_samp.max()
        S_norm = S_samp / peak_val if peak_val > 0 else S_samp
        fft_freqs_out = target_f.tolist()
        fft_amps_out = [round(float(v), 4) for v in S_norm]
    res_hz = float(sr) / WINDOW_SIZE
    a4_median = float(np.median(fv))
    a4_std    = float(np.std(fv))
    return {
        "success": True,
        "peak_freq": round(a4, 4), "peak_freq_median": round(a4_median, 4), "peak_freq_std": round(a4_std, 4),
        "n_votes": len(votes), "cents_vs_432": round(c432, 3), "cents_vs_440": round(c440, 3),
        "is_432": is_432, "verdict": verdict,
        "a4_weighted": round(a4, 4), "a4_median": round(a4_median, 4), "a4_std": round(a4_std, 4),
        "fft_freqs": fft_freqs_out, "fft_amplitudes": fft_amps_out,
        "window_size": WINDOW_SIZE, "sample_rate": sr, "resolution_hz": round(res_hz, 4),
        "cert_threshold_cents": CERT_THRESHOLD_CENTS,
        "algorithm": "FFT Hann window + parabolic interpolation + octave folding + amplitude-weighted average",
    }

def _measure_a4_streaming(samples, sr):
    """Generator: yield per-window FFT data (200-600 Hz) as analysis converges.
    Each window message includes an independent A4 estimate and timestamp.
    Final yield is {"type":"done","result":{...}} with per_window stats.
    """
    import time

    DISP_MIN, DISP_MAX, DISP_PTS = 200.0, 600.0, 250
    total_samples  = len(samples)
    duration_sec   = total_samples / sr

    positions = np.linspace(
        WINDOW_SIZE, max(WINDOW_SIZE + 1, total_samples - WINDOW_SIZE), N_WINDOWS
    ).astype(int)

    spec_acc       = None
    Fm_ref         = None
    spec_cnt       = 0
    acc_votes      = []   # accumulated across windows → running a4_est
    per_window_data = []
    res            = None

    for i, pos in enumerate(positions):
        chunk = samples[pos : pos + WINDOW_SIZE]
        if len(chunk) < WINDOW_SIZE:
            continue

        S   = np.abs(rfft(chunk * np.hanning(WINDOW_SIZE)))
        F   = rfftfreq(WINDOW_SIZE, 1.0 / sr)
        res = float(F[1] - F[0])

        mask = (F >= 50) & (F <= 2000)
        Sm, Fm = S[mask], F[mask]

        if spec_acc is None:
            spec_acc = Sm.copy(); Fm_ref = Fm
        else:
            spec_acc = spec_acc + Sm
        spec_cnt += 1

        # Peak detection — used for both per-window and accumulated estimates
        win_votes = []
        if Sm.max() > 0:
            peaks, _ = find_peaks(Sm, height=Sm.max() * 0.01,
                                  distance=int(20 / res))
            if len(peaks):
                pf = Fm[peaks]; pa = Sm[peaks]
                order = np.argsort(pa)[::-1]
                pf, pa = pf[order[:15]], pa[order[:15]]
                for freq, amp in zip(pf, pa):
                    f4 = freq
                    while f4 < A4_MIN: f4 *= 2
                    while f4 > A4_MAX: f4 /= 2
                    if A4_MIN <= f4 <= A4_MAX:
                        win_votes.append((f4, float(amp)))
                        acc_votes.append((f4, float(amp)))

        # Independent per-window A4 estimate
        window_a4 = None
        if win_votes:
            fv = np.array([v[0] for v in win_votes])
            av = np.array([v[1] for v in win_votes])
            window_a4 = round(float(np.average(fv, weights=av)), 3)

        timestamp = round(float(pos / sr), 2)
        per_window_data.append({"idx": i, "time": timestamp, "a4": window_a4})

        # Accumulated A4 estimate (running average across all windows so far)
        a4_est = None
        if acc_votes:
            fv = np.array([v[0] for v in acc_votes])
            av = np.array([v[1] for v in acc_votes])
            a4_est = float(np.average(fv, weights=av))

        # Display spectrum: accumulated average, 200-600 Hz, fixed grid
        S_avg   = spec_acc / spec_cnt
        t_freqs = np.linspace(DISP_MIN, DISP_MAX, DISP_PTS)
        S_disp  = np.interp(t_freqs, Fm_ref, S_avg)
        peak_v  = S_disp.max()
        S_norm  = S_disp / peak_v if peak_v > 0 else S_disp

        yield {
            "type":          "window",
            "window_idx":    i,
            "total_windows": N_WINDOWS,
            "freqs":         [round(float(f), 2) for f in t_freqs],
            "amps":          [round(float(a), 4) for a in S_norm],
            "a4_estimate":   round(a4_est, 3) if a4_est is not None else None,
            "window_a4":     window_a4,
            "timestamp":     timestamp,
            "duration_sec":  round(duration_sec, 2),
        }
        time.sleep(0.12)

    result = _measure_a4(samples, sr)
    result["per_window"]       = per_window_data
    result["duration_analyzed"] = round(duration_sec, 2)
    valid = [d["a4"] for d in per_window_data if d["a4"] is not None]
    if len(valid) >= 2:
        result["std_dev_hz"]  = round(float(np.std(valid)), 4)
        result["mean_hz"]     = round(float(np.mean(valid)), 4)
    else:
        result["std_dev_hz"]  = None
        result["mean_hz"]     = None
    yield {"type": "done", "result": result}


LARGE_FILE_BYTES = 50 * 1024 * 1024   # 50 MB
LARGE_FILE_SECS  = 600                  # 10 minutes
N_SAMPLES        = 20
SAMPLE_DUR       = 5                    # seconds per sample

def _get_duration(input_path):
    """Return audio duration in seconds via ffprobe, or None on error."""
    import json as _json
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", input_path],
            capture_output=True, check=True, timeout=30
        )
        return float(_json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None

def _is_large_file(input_path):
    """True if file size >50 MB or audio duration >10 minutes."""
    if os.path.getsize(input_path) > LARGE_FILE_BYTES:
        return True
    dur = _get_duration(input_path)
    return dur is not None and dur > LARGE_FILE_SECS

def _load_as_wav_sampled(input_path, tmp_wav, channels=1,
                          n_samples=N_SAMPLES, sample_dur=SAMPLE_DUR):
    """Extract n_samples × sample_dur seconds distributed across the full file
    (stratified random sampling), concatenate raw PCM and write as a single WAV.
    Falls back to full load if duration cannot be probed or file is very short."""
    import random as _random
    duration = _get_duration(input_path)
    if duration is None or duration <= sample_dur * 2:
        _load_as_wav(input_path, tmp_wav, channels=channels)
        return

    max_start = duration - sample_dur
    # Stratified sampling: divide [0, max_start] into n_samples equal intervals
    interval = max_start / n_samples
    offsets = sorted([
        _random.uniform(i * interval, min((i + 1) * interval, max_start))
        for i in range(n_samples)
    ])

    all_pcm = bytearray()
    for offset in offsets:
        r = subprocess.run([
            "ffmpeg", "-y",
            "-ss", f"{offset:.3f}", "-t", str(sample_dur),
            "-i", input_path,
            "-ac", str(channels), "-ar", str(SR_ANALYSIS),
            "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
            "-loglevel", "error"
        ], capture_output=True, check=True, timeout=60)
        all_pcm.extend(r.stdout)

    with wave.open(tmp_wav, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)   # 16-bit
        wf.setframerate(SR_ANALYSIS)
        wf.writeframes(bytes(all_pcm))

def _load_as_wav(input_path, tmp_wav, channels=1, max_seconds=None):
    cmd = ["ffmpeg", "-y", "-i", input_path]
    if max_seconds is not None:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-ac", str(channels), "-ar", str(SR_ANALYSIS), "-c:a", "pcm_s16le", tmp_wav, "-loglevel", "error"]
    subprocess.run(cmd, check=True, capture_output=True)

def _read_wav_samples(wav_path):
    with wave.open(wav_path,"rb") as wf:
        sr = wf.getframerate(); raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float64)/32768.0, sr

def analyze_file(input_path):
    tmp_wav = tempfile.mktemp(suffix=".wav")
    try:
        _load_as_wav(input_path, tmp_wav, channels=1)
        samples, sr = _read_wav_samples(tmp_wav)
        result = _measure_a4(samples, sr)
        result["filename"] = os.path.basename(input_path)
        return result
    except subprocess.CalledProcessError as e:
        return {"success": False, "error": f"ffmpeg error: {e.stderr.decode(errors='replace')[:300]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if os.path.exists(tmp_wav): os.remove(tmp_wav)

def _find_tool(name):
    """Trova un tool nell'ordine: shutil.which → subprocess which → /usr/bin hardcoded."""
    import shutil as _sh
    p = _sh.which(name)
    if p:
        return p
    try:
        r = subprocess.run(["which", name], capture_output=True, text=True, timeout=5)
        p = r.stdout.strip()
        if p:
            return p
    except Exception:
        pass
    hardcoded = f"/usr/bin/{name}"
    if os.path.isfile(hardcoded):
        return hardcoded
    return None

def _rb_version(rb_path):
    """Ritorna la versione di rubberband come stringa, o '?' se non leggibile."""
    try:
        r = subprocess.run([rb_path, "--version"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or r.stderr.strip() or "?"
    except Exception:
        return "?"

def _pitch_shift(input_wav, output_wav, shift_cents, timeout, engine_pref="rubberband"):
    """Pitch shift con 3 engine in ordine di priorità:
    1. rubberband standalone (R3)
    2. ffmpeg con filtro rubberband (librubberband interna)
    3. sox (fallback finale)
    Ritorna il nome dell'engine usato o solleva un'eccezione."""
    import time as _t
    rb_path     = _find_tool("rubberband")
    ffmpeg_path = _find_tool("ffmpeg")
    sox_path    = _find_tool("sox")

    semitones   = shift_cents / 100.0
    pitch_scale = 2.0 ** (shift_cents / 1200.0)

    # ── 1. Rubberband standalone ──────────────────────────────────────────────
    if rb_path and engine_pref in ("rubberband", "ffmpeg-rubberband"):
        rb_ver = _rb_version(rb_path)
        try:
            major, minor = [int(x) for x in rb_ver.split(".")[:2]]
            r3_supported = (major > 3) or (major == 3 and minor >= 3)
        except Exception:
            r3_supported = False
        if r3_supported:
            rb_args = [rb_path, "-3", "--ignore-clipping", "--pitch", f"{semitones:.6f}", input_wav, output_wav]
            rb_mode = f"R3 (v{rb_ver})"
        else:
            rb_args = [rb_path, "--fine", "--pitch", f"{semitones:.6f}", input_wav, output_wav]
            rb_mode = f"--fine (v{rb_ver})"
        print(f"[pitch_shift] 1/3 rubberband {rb_mode}  semitones={semitones:+.6f}  cents={shift_cents:+.4f}  cmd={' '.join(rb_args)}", flush=True)
        t0 = _t.monotonic()
        try:
            result = subprocess.run(rb_args, capture_output=True, timeout=timeout)
            elapsed = _t.monotonic() - t0
            rb_stderr = result.stderr.decode(errors="replace")[:1000] if result.stderr else ""
            print(f"[pitch_shift] rubberband rc={result.returncode}  time={elapsed:.2f}s  stderr={rb_stderr}", flush=True)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, rb_args, result.stdout, result.stderr)
            if not os.path.exists(output_wav) or os.path.getsize(output_wav) <= 44:
                raise RuntimeError(f"output mancante/vuoto size={os.path.getsize(output_wav) if os.path.exists(output_wav) else 0}")
            print(f"[pitch_shift] WINNER: rubberband  time={elapsed:.2f}s  output_size={os.path.getsize(output_wav)}", flush=True)
            return "rubberband"
        except Exception as e:
            elapsed = _t.monotonic() - t0
            stderr = e.stderr.decode(errors="replace")[:1000] if hasattr(e, "stderr") and e.stderr else str(e)
            print(f"[pitch_shift] rubberband FAILED  time={elapsed:.2f}s  {type(e).__name__}: {stderr}", flush=True)
            if os.path.exists(output_wav):
                os.remove(output_wav)

    # ── 2. ffmpeg con filtro rubberband (librubberband) ───────────────────────
    if ffmpeg_path:
        af_filter = f"rubberband=pitch={pitch_scale:.8f}:pitchq=quality"
        ff_args = [ffmpeg_path, "-y", "-i", input_wav, "-af", af_filter, output_wav, "-loglevel", "error"]
        print(f"[pitch_shift] 2/3 ffmpeg-rubberband  scale={pitch_scale:.8f}  cents={shift_cents:+.4f}  cmd={' '.join(ff_args)}", flush=True)
        t0 = _t.monotonic()
        try:
            result = subprocess.run(ff_args, capture_output=True, timeout=timeout)
            elapsed = _t.monotonic() - t0
            ff_stderr = result.stderr.decode(errors="replace")[:1000] if result.stderr else ""
            print(f"[pitch_shift] ffmpeg-rubberband rc={result.returncode}  time={elapsed:.2f}s  stderr={ff_stderr}", flush=True)
            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, ff_args, result.stdout, result.stderr)
            if not os.path.exists(output_wav) or os.path.getsize(output_wav) <= 44:
                raise RuntimeError(f"output mancante/vuoto size={os.path.getsize(output_wav) if os.path.exists(output_wav) else 0}")
            print(f"[pitch_shift] WINNER: ffmpeg-rubberband  time={elapsed:.2f}s  output_size={os.path.getsize(output_wav)}", flush=True)
            return "ffmpeg-rubberband"
        except Exception as e:
            elapsed = _t.monotonic() - t0
            stderr = e.stderr.decode(errors="replace")[:1000] if hasattr(e, "stderr") and e.stderr else str(e)
            print(f"[pitch_shift] ffmpeg-rubberband FAILED  time={elapsed:.2f}s  {type(e).__name__}: {stderr}", flush=True)
            if os.path.exists(output_wav):
                os.remove(output_wav)

    # ── 3. SoX (fallback finale) ─────────────────────────────────────────────
    if sox_path:
        sox_args = [sox_path, input_wav, output_wav, "pitch", f"{shift_cents:.4f}"]
        print(f"[pitch_shift] 3/3 sox  cents={shift_cents:+.4f}  cmd={' '.join(sox_args)}", flush=True)
        t0 = _t.monotonic()
        subprocess.run(sox_args, check=True, capture_output=True, timeout=timeout)
        elapsed = _t.monotonic() - t0
        print(f"[pitch_shift] WINNER: sox  time={elapsed:.2f}s  output_size={os.path.getsize(output_wav)}", flush=True)
        return "sox"

    raise RuntimeError("Nessun engine di pitch shift disponibile (rubberband, ffmpeg, sox non trovati)")

def convert_to_432(input_path, output_path, max_seconds=None, sox_timeout=None):
    rb_path     = _find_tool("rubberband")
    ffmpeg_path = _find_tool("ffmpeg")
    sox_path    = _find_tool("sox")
    print(f"[convert] start  rubberband={rb_path}  ffmpeg={ffmpeg_path}  sox={sox_path}  in={input_path}", flush=True)
    if not rb_path and not ffmpeg_path and not sox_path:
        return {"success": False, "error": "Nessun engine pitch shift trovato (rubberband / ffmpeg / sox)"}

    tmp_in  = tempfile.mktemp(suffix=".wav")
    tmp_432 = tempfile.mktemp(suffix=".wav")
    engine_used = "unknown"
    try:
        _load_as_wav(input_path, tmp_in, channels=1, max_seconds=max_seconds)
        tmp_in_size = os.path.getsize(tmp_in) if os.path.exists(tmp_in) else -1
        print(f"[convert] decode_done  tmp_in_size={tmp_in_size}", flush=True)
        if tmp_in_size <= 0:
            return {"success": False, "error": "Decodifica WAV fallita: file vuoto"}
        samples, sr = _read_wav_samples(tmp_in)
        pre = _measure_a4(samples, sr)
        print(f"[convert] pre_analysis  success={pre.get('success')}  peak={pre.get('peak_freq')}  is_432={pre.get('is_432')}", flush=True)
        if not pre["success"]:
            return {"success": False, "error": f"Analisi pre fallita: {pre.get('error')}"}
        a4_original = pre["peak_freq"]
        if pre["is_432"]:
            return {"success": True, "already_432": True, "pre_freq": a4_original, "pre_cents": pre["cents_vs_432"], "post_freq": a4_original, "post_cents": pre["cents_vs_432"], "shift_applied": 0.0, "certified": True, "engine": "none", "message": "Il brano è già certificato a 432 Hz."}
        shift_cents = 1200.0 * math.log2(TARGET_HZ / a4_original)

        try:
            engine_used = _pitch_shift(tmp_in, tmp_432, shift_cents, sox_timeout)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace")[:400] if e.stderr else "(nessun stderr)"
            print(f"[convert] pitch_shift_ERROR  returncode={e.returncode}  stderr={stderr}", flush=True)
            raise
        tmp_432_size = os.path.getsize(tmp_432) if os.path.exists(tmp_432) else -1
        print(f"[convert] pitch_shift_done  engine={engine_used}  tmp_432_size={tmp_432_size}", flush=True)
        if tmp_432_size <= 0:
            return {"success": False, "error": f"{engine_used} ha prodotto un file vuoto"}

        ext = os.path.splitext(output_path)[1].lower()
        print(f"[convert] ffmpeg_encode  ext={ext}  out={output_path}", flush=True)
        if ext == ".mp3":
            subprocess.run(["ffmpeg","-y","-i",tmp_432,"-ac","2","-c:a","libmp3lame","-qscale:a","2",output_path,"-loglevel","error"],
                           check=True, capture_output=True)
        elif ext == ".m4a":
            subprocess.run(["ffmpeg","-y","-i",tmp_432,"-ac","2","-c:a","aac","-b:a","256k","-movflags","+faststart","-f","mp4",output_path,"-loglevel","error"],
                           check=True, capture_output=True)
        else:
            return {"success": False, "error": f"Formato non supportato: {ext}"}
        out_size = os.path.getsize(output_path) if os.path.exists(output_path) else -1
        print(f"[convert] ffmpeg_done  out_size={out_size}", flush=True)
        if out_size <= 0:
            return {"success": False, "error": "ffmpeg ha prodotto un file vuoto"}

        tmp_post = tempfile.mktemp(suffix=".wav")
        try:
            _load_as_wav(output_path, tmp_post, channels=1)
            samples_post, sr_post = _read_wav_samples(tmp_post)
            post = _measure_a4(samples_post, sr_post)
        finally:
            if os.path.exists(tmp_post): os.remove(tmp_post)
        if not post["success"]:
            return {"success": False, "error": f"Verifica post fallita: {post.get('error')}"}
        post_cents = post["cents_vs_432"]
        certified  = abs(post_cents) < CERT_THRESHOLD_CENTS
        correction_passes = 1
        corr_pass_error = None
        # Sanity check: se lo shift era negativo (abbassare pitch) ma post > pre, qualcosa è andato storto
        wrong_direction = (shift_cents < 0 and post["peak_freq"] > a4_original * 1.001) or \
                          (shift_cents > 0 and post["peak_freq"] < a4_original * 0.999)
        print(f"[convert] post_freq={post['peak_freq']:.4f} Hz  post_cents={post_cents:+.4f}  "
              f"certified={certified}  wrong_direction={wrong_direction}  "
              f"second_pass_trigger={not certified and abs(post_cents) < 30.0}", flush=True)
        if wrong_direction:
            print(f"[convert] WARNING: shift_cents={shift_cents:+.4f} ma post_freq={post['peak_freq']:.4f} > pre_freq={a4_original:.4f} — "
                  "possibile bug SoX pitch direction o canali audio", flush=True)

        # Corrective second pass: solo per SoX (impreciso).
        # Rubber Band è preciso — il secondo passaggio peggiora il risultato.
        if not certified and abs(post_cents) < 30.0 and engine_used == "sox":
            tmp_corr_out = tempfile.mktemp(suffix=".wav")
            try:
                eff = (shift_cents + post_cents) / shift_cents
                if abs(eff) < 0.05:
                    raise ValueError(f"Efficienza pitch shift non plausibile: {eff:.4f}")
                shift_compensated = shift_cents / eff
                max_shift = abs(shift_cents) * 3.0
                shift_compensated = max(-max_shift, min(max_shift, shift_compensated))
                print(f"[convert] >>> second pass: eff={eff:.4f}  shift_compensated={shift_compensated:+.4f} cent", flush=True)
                engine_used = _pitch_shift(tmp_in, tmp_corr_out, shift_compensated, sox_timeout, engine_pref=engine_used)
                if ext == ".mp3":
                    subprocess.run(["ffmpeg","-y","-i",tmp_corr_out,"-ac","2","-c:a","libmp3lame","-qscale:a","2",output_path,"-loglevel","error"], check=True, capture_output=True)
                elif ext == ".m4a":
                    subprocess.run(["ffmpeg","-y","-i",tmp_corr_out,"-ac","2","-c:a","aac","-b:a","256k","-movflags","+faststart","-f","mp4",output_path,"-loglevel","error"], check=True, capture_output=True)
                tmp_post2 = tempfile.mktemp(suffix=".wav")
                try:
                    _load_as_wav(output_path, tmp_post2, channels=1)
                    samples2, sr2 = _read_wav_samples(tmp_post2)
                    post2 = _measure_a4(samples2, sr2)
                finally:
                    if os.path.exists(tmp_post2): os.remove(tmp_post2)
                if post2["success"]:
                    post           = post2
                    post_cents     = post2["cents_vs_432"]
                    certified      = abs(post_cents) < CERT_THRESHOLD_CENTS
                    correction_passes = 2
                    print(f"[convert] >>> second pass result: post2_cents={post_cents:+.4f}  certified={certified}", flush=True)
                else:
                    corr_pass_error = f"measure_a4 failed: {post2.get('error')}"
                    print(f"[convert] >>> second pass FAILED: {corr_pass_error}", flush=True)
            except subprocess.CalledProcessError as e:
                corr_pass_error = f"subprocess: {e.stderr.decode(errors='replace')[:200] if e.stderr else str(e)}"
                print(f"[convert] >>> second pass CalledProcessError: {corr_pass_error}", flush=True)
            except Exception as e:
                corr_pass_error = str(e)
                print(f"[convert] >>> second pass Exception: {corr_pass_error}", flush=True)
            finally:
                if os.path.exists(tmp_corr_out): os.remove(tmp_corr_out)

        message = (f"Certificato a 432 Hz \u2713 (pass {correction_passes}, \u0394 {post_cents:+.2f} cent)"
                   if certified else
                   f"Conversione completata, verifica manuale consigliata (pass {correction_passes}, \u0394 {post_cents:+.2f} cent)")
        result = {"success": True, "already_432": False, "pre_freq": a4_original,
                  "pre_cents_vs_432": round(pre["cents_vs_432"],3), "pre_cents_vs_440": round(pre["cents_vs_440"],3),
                  "pre_verdict": pre["verdict"], "shift_applied": round(shift_cents,4),
                  "post_freq": post["peak_freq"], "post_cents_vs_432": round(post_cents,3),
                  "post_verdict": post["verdict"], "certified": certified,
                  "correction_passes": correction_passes, "target_hz": TARGET_HZ,
                  "engine": engine_used, "message": message}
        if corr_pass_error:
            result["corr_pass_error"] = corr_pass_error
        return result
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace")[:400] if e.stderr else "(nessun stderr)"
        print(f"[convert] FATAL CalledProcessError  returncode={e.returncode}  stderr={stderr}", flush=True)
        return {"success": False, "error": f"Errore processo (returncode={e.returncode}): {stderr}"}
    except Exception as e:
        import traceback
        print(f"[convert] FATAL Exception:\n{traceback.format_exc()}", flush=True)
        return {"success": False, "error": str(e)}
    finally:
        for f in [tmp_in, tmp_432]:
            if os.path.exists(f): os.remove(f)
