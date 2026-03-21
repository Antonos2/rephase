#!/usr/bin/env python3
import os, wave, tempfile, subprocess
import numpy as np

def convert_to_432(input_path, output_path):
    tmp_wav = tempfile.mktemp(suffix='.wav')
    tmp_432 = tempfile.mktemp(suffix='.wav')
    try:
        subprocess.run(['ffmpeg','-y','-i',input_path,'-ac','2','-ar','48000','-c:a','pcm_s16le',tmp_wav,'-loglevel','error'], check=True)
        subprocess.run(['sox', tmp_wav, tmp_432, 'pitch', '-31.767'], check=True)
        ext = os.path.splitext(output_path)[1].lower()
        if ext == '.mp3':
            subprocess.run(['ffmpeg','-y','-i',tmp_432,'-c:a','libmp3lame','-qscale:a','2',output_path,'-loglevel','error'], check=True)
        elif ext == '.m4a':
            subprocess.run(['ffmpeg','-y','-i',tmp_432,'-c:a','aac','-b:a','256k','-movflags','+faststart','-f','mp4',output_path,'-loglevel','error'], check=True)
        return {"success": True, "converted_freq": 432.0, "shift_cents": -31.767}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        for f in [tmp_wav, tmp_432]:
            if os.path.exists(f): os.remove(f)

def analyze_file(input_path):
    tmp_wav = tempfile.mktemp(suffix='.wav')
    try:
        subprocess.run(['ffmpeg','-y','-i',input_path,'-ac','1','-ar','44100','-c:a','pcm_s16le',tmp_wav,'-loglevel','error'], check=True, capture_output=True)
        with wave.open(tmp_wav,'rb') as wf:
            sr=wf.getframerate(); raw=wf.readframes(wf.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)/32768.0
        fft_size=65536; target=int(30*sr)
        seg = samples[(len(samples)-target)//2:(len(samples)-target)//2+target] if len(samples)>target else samples
        n=min(fft_size,len(seg)); seg=seg[:n]
        spectrum=np.abs(np.fft.rfft(seg*np.hanning(n),n=fft_size))
        freqs=np.fft.rfftfreq(fft_size,d=1.0/sr)
        mask=(freqs>=200)&(freqs<=2000); idx=np.where(mask)[0]
        pi=idx[np.argmax(spectrum[idx])]; pf=freqs[pi]
        if 0<pi<len(spectrum)-1:
            y0,y1,y2=spectrum[pi-1],spectrum[pi],spectrum[pi+1]; d=(2*y1-y0-y2)
            if d!=0: pf+=0.5*(y2-y0)/d*(sr/fft_size)
        c432=1200*np.log2(pf/432.0); c440=1200*np.log2(pf/440.0)
        is_432=bool(abs(c432)<abs(c440))
        return {"success":True,"peak_freq":round(float(pf),2),"cents_vs_432":round(float(c432),1),"cents_vs_440":round(float(c440),1),"is_432":is_432,"verdict":"432 Hz" if is_432 else "440 Hz"}
    except Exception as e:
        return {"success":False,"error":str(e)}
    finally:
        if os.path.exists(tmp_wav): os.remove(tmp_wav)
