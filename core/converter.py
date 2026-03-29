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
    Final yield is {"type":"done","result":{...full _measure_a4 output...}}.
    Pacing: ~120 ms sleep between windows for smooth 150 ms visual cadence.
    Data is scientifically real — accumulated average spectrum per window.
    """
    import time

    DISP_MIN, DISP_MAX, DISP_PTS = 200.0, 600.0, 250
    positions = np.linspace(
        WINDOW_SIZE, max(WINDOW_SIZE + 1, len(samples) - WINDOW_SIZE), N_WINDOWS
    ).astype(int)

    spec_acc  = None
    Fm_ref    = None
    spec_cnt  = 0
    votes     = []
    res       = None

    for i, pos in enumerate(positions):
        chunk = samples[pos : pos + WINDOW_SIZE]
        if len(chunk) < WINDOW_SIZE:
            continue

        S  = np.abs(rfft(chunk * np.hanning(WINDOW_SIZE)))
        F  = rfftfreq(WINDOW_SIZE, 1.0 / sr)
        res = float(F[1] - F[0])

        mask = (F >= 50) & (F <= 2000)
        Sm, Fm = S[mask], F[mask]

        if spec_acc is None:
            spec_acc = Sm.copy(); Fm_ref = Fm
        else:
            spec_acc = spec_acc + Sm
        spec_cnt += 1

        # Peak detection → A4 votes
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
                        votes.append((f4, float(amp)))

        # Current A4 estimate (amplitude-weighted)
        a4_est = None
        if votes:
            fv = np.array([v[0] for v in votes])
            av = np.array([v[1] for v in votes])
            a4_est = float(np.average(fv, weights=av))

        # Display spectrum: accumulated average, 200-600 Hz, fixed grid
        S_avg  = spec_acc / spec_cnt
        t_freqs = np.linspace(DISP_MIN, DISP_MAX, DISP_PTS)
        S_disp  = np.interp(t_freqs, Fm_ref, S_avg)
        peak_v  = S_disp.max()
        S_norm  = S_disp / peak_v if peak_v > 0 else S_disp

        yield {
            "type": "window",
            "window_idx": i,
            "total_windows": N_WINDOWS,
            "freqs": [round(float(f), 2) for f in t_freqs],
            "amps":  [round(float(a), 4) for a in S_norm],
            "a4_estimate": round(a4_est, 3) if a4_est is not None else None,
        }
        time.sleep(0.12)   # ~120 ms pacing → ~150 ms cadence per frame

    result = _measure_a4(samples, sr)
    yield {"type": "done", "result": result}


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

def convert_to_432(input_path, output_path):
    tmp_in  = tempfile.mktemp(suffix=".wav")
    tmp_432 = tempfile.mktemp(suffix=".wav")
    try:
        _load_as_wav(input_path, tmp_in, channels=2)
        samples, sr = _read_wav_samples(tmp_in)
        pre = _measure_a4(samples, sr)
        if not pre["success"]:
            return {"success": False, "error": f"Analisi pre fallita: {pre.get('error')}"}
        a4_original = pre["peak_freq"]
        if pre["is_432"]:
            return {"success": True, "already_432": True, "pre_freq": a4_original, "pre_cents": pre["cents_vs_432"], "post_freq": a4_original, "post_cents": pre["cents_vs_432"], "shift_applied": 0.0, "certified": True, "message": "Il brano è già certificato a 432 Hz."}
        shift_cents = 1200.0 * math.log2(TARGET_HZ / a4_original)
        subprocess.run(["sox", tmp_in, tmp_432, "pitch", f"{shift_cents:.4f}"], check=True, capture_output=True)
        ext = os.path.splitext(output_path)[1].lower()
        if ext == ".mp3":
            subprocess.run(["ffmpeg","-y","-i",tmp_432,"-c:a","libmp3lame","-qscale:a","2",output_path,"-loglevel","error"], check=True, capture_output=True)
        elif ext == ".m4a":
            subprocess.run(["ffmpeg","-y","-i",tmp_432,"-c:a","aac","-b:a","256k","-movflags","+faststart","-f","mp4",output_path,"-loglevel","error"], check=True, capture_output=True)
        else:
            return {"success": False, "error": f"Formato non supportato: {ext}"}
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
        print(f"[convert] post_freq={post['peak_freq']:.4f} Hz  post_cents={post_cents:+.4f}  certified={certified}  second_pass_trigger={not certified and abs(post_cents) < 30.0}", flush=True)
        # Corrective second pass: re-apply from original WAV with compensated shift.
        # SoX applies only a fraction of the requested pitch shift (non-linear).
        # efficiency = actual_shift / requested_shift = (shift_cents + post_cents) / shift_cents
        # Compensated shift = shift_cents / efficiency, applied fresh on tmp_in (no lossy re-encode chain).
        if not certified and abs(post_cents) < 30.0:
            tmp_corr_out = tempfile.mktemp(suffix=".wav")
            try:
                eff = (shift_cents + post_cents) / shift_cents
                if abs(eff) < 0.05:
                    raise ValueError(f"Efficienza SoX non plausibile: {eff:.4f}")
                shift_compensated = shift_cents / eff
                # Clamp: non più di 3× lo shift originale per sicurezza
                max_shift = abs(shift_cents) * 3.0
                shift_compensated = max(-max_shift, min(max_shift, shift_compensated))
                print(f"[convert] >>> second pass (from original): eff={eff:.4f}  shift_compensated={shift_compensated:+.4f} cent", flush=True)
                subprocess.run(["sox", tmp_in, tmp_corr_out, "pitch", f"{shift_compensated:.4f}"], check=True, capture_output=True)
                if ext == ".mp3":
                    subprocess.run(["ffmpeg","-y","-i",tmp_corr_out,"-c:a","libmp3lame","-qscale:a","2",output_path,"-loglevel","error"], check=True, capture_output=True)
                elif ext == ".m4a":
                    subprocess.run(["ffmpeg","-y","-i",tmp_corr_out,"-c:a","aac","-b:a","256k","-movflags","+faststart","-f","mp4",output_path,"-loglevel","error"], check=True, capture_output=True)
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
                  "correction_passes": correction_passes, "target_hz": TARGET_HZ, "message": message}
        if corr_pass_error:
            result["corr_pass_error"] = corr_pass_error
        return result
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace")[:400] if e.stderr else ""
        return {"success": False, "error": f"Errore processo: {stderr}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        for f in [tmp_in, tmp_432]:
            if os.path.exists(f): os.remove(f)
