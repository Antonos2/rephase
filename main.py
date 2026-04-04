#!/usr/bin/env python3
# Rephase API — pitch engine: Rubber Band R3 → ffmpeg librubberband → SoX
import os, uuid, threading
import stripe
import time as _time
import hashlib
import logging
import psutil
import aiofiles
from pathlib import Path
from collections import deque
from datetime import datetime, timezone
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Depends, Header, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from core.converter import convert_to_432, analyze_file

# ══════════════════════════════════════════════════════════════════════════════
# 🚦 VIGILE URBANO — logger interno Rephase
# ══════════════════════════════════════════════════════════════════════════════

# Log strutturato su file (rotazione manuale ogni 5000 eventi)
_log_handler = logging.FileHandler("rephase_events.log")
_log_handler.setFormatter(logging.Formatter("%(message)s"))
_vigile = logging.getLogger("vigile")
_vigile.setLevel(logging.INFO)
_vigile.addHandler(_log_handler)
_vigile.propagate = False

# Deque in memoria — ultimi 5000 eventi (zero memoria aggiuntiva rilevante)
_event_log: deque = deque(maxlen=5000)

# Contatori globali
_active_conversions: int = 0
_active_verifies: int = 0
_active_analyses: int = 0
_counters_lock = threading.Lock()


def _log_event(
    event_type: str,        # "verify" | "convert" | "convert_sync" | "analyze"
    esito: str,             # "ok" | "error" | "timeout" | "already_432"
    duration_sec: float,
    file_size_mb: float,
    formato: str,
    request: Request,
    extra: dict = None,
):
    """Registra un evento nel log interno. Chiamato nel finally di ogni endpoint."""
    ip_raw = ""
    try:
        ip_raw = request.client.host or ""
    except Exception:
        pass
    ip_hash = hashlib.sha256(ip_raw.encode()).hexdigest()[:12]

    try:
        mem_pct = psutil.virtual_memory().percent
        cpu_pct = psutil.cpu_percent(interval=None)   # non-blocking
    except Exception:
        mem_pct = cpu_pct = -1.0

    with _counters_lock:
        active_snap = {
            "conversions": _active_conversions,
            "verifies":    _active_verifies,
            "analyses":    _active_analyses,
        }

    entry = {
        "ts":           datetime.now(timezone.utc).isoformat(),
        "type":         event_type,
        "esito":        esito,
        "duration_sec": round(duration_sec, 2),
        "file_mb":      round(file_size_mb, 3),
        "format":       formato,
        "ip_hash":      ip_hash,
        "ua":           (request.headers.get("user-agent", "") or "")[:80],
        "active":       active_snap,
        "mem_pct":      mem_pct,
        "cpu_pct":      cpu_pct,
        "extra":        extra or {},
    }

    _event_log.append(entry)
    _vigile.info(entry)


# ── In-memory job store (analysis only) ──────────────────────────────────────
_jobs: dict = {}
_jobs_lock = threading.Lock()


def _cleanup_jobs():
    """Background thread: remove completed/failed analysis jobs older than 30 min."""
    while True:
        _time.sleep(300)
        cutoff = _time.time() - 1800
        with _jobs_lock:
            expired = [k for k, v in _jobs.items()
                       if v.get("status") in ("done", "error")
                       and v.get("last_accessed", v.get("created_at", 0)) < cutoff]
            for k in expired:
                del _jobs[k]


threading.Thread(target=_cleanup_jobs, daemon=True).start()

# ── Startup diagnostic: log sox/ffmpeg paths immediately ─────────────────────
import shutil as _shutil, subprocess as _sp_startup
def _log_tool(name):
    p = _shutil.which(name)
    if not p:
        try:
            r = _sp_startup.run(["which", name], capture_output=True, text=True, timeout=5)
            p = r.stdout.strip() or None
        except Exception:
            pass
    print(f"[startup] {name}: {p or 'NOT FOUND'}", flush=True)
_log_tool("rubberband")
_log_tool("sox")
_log_tool("ffmpeg")

app = FastAPI(title="Rephase API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TEMP_DIR = Path("/tmp/rephase")
TEMP_DIR.mkdir(exist_ok=True)
ALLOWED = {".mp3",".m4a",".wav",".flac",".aac",".aiff"}
MAX_SIZE = 500*1024*1024

def cleanup(path):
    import time; time.sleep(60)
    if os.path.exists(path): os.remove(path)

# ── Job system per conversione asincrona ──────────────────────────────────────
import json as _json
JOBS_DIR = TEMP_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

def _job_path(job_id):
    return JOBS_DIR / f"{job_id}.json"

def _save_job(job_id, data):
    with open(_job_path(job_id), "w") as f:
        _json.dump(data, f)

def _load_job(job_id):
    p = _job_path(job_id)
    if not p.exists():
        return None
    with open(p) as f:
        return _json.load(f)

def _run_conversion(job_id, tmp_in, tmp_out, fmt, filename, file_mb, duration_sec, sox_timeout, cleanup_input=True):
    """Eseguita in background thread — aggiorna il job file."""
    try:
        _save_job(job_id, {"status": "analyzing", "progress": 10})
        pre_check = analyze_file(tmp_in)
        if pre_check.get("is_432"):
            _save_job(job_id, {
                "status": "done", "already_432": True,
                "peak_freq": pre_check["peak_freq"],
                "cents_vs_432": pre_check["cents_vs_432"],
            })
            if cleanup_input and os.path.exists(tmp_in): os.remove(tmp_in)
            return

        _save_job(job_id, {"status": "converting", "progress": 30})
        result = convert_to_432(tmp_in, tmp_out, max_seconds=360, sox_timeout=sox_timeout)

        if not result["success"]:
            _save_job(job_id, {"status": "error", "error": result.get("error", "Errore sconosciuto")})
            return

        _save_job(job_id, {
            "status": "done",
            "already_432": False,
            "filename": filename,
            "format": fmt,
            "output_path": tmp_out,
            "pre_freq": round(result.get("pre_freq", 0), 4),
            "shift_applied": round(result.get("shift_applied", 0), 4),
            "post_freq": round(result.get("post_freq", 0), 4),
            "post_cents_vs_432": round(result.get("post_cents_vs_432", 0), 4),
            "certified": result.get("certified", False),
            "correction_passes": result.get("correction_passes", 1),
            "engine": result.get("engine", "unknown"),
            "corr_pass_error": result.get("corr_pass_error"),
            "audio_duration_sec": round(duration_sec, 1),
        })
    except Exception as e:
        import traceback
        print(f"[convert/job] {job_id} FATAL:\n{traceback.format_exc()}", flush=True)
        _save_job(job_id, {"status": "error", "error": str(e)})
    finally:
        if cleanup_input and os.path.exists(tmp_in):
            try: os.remove(tmp_in)
            except Exception: pass

@app.get("/")
def root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/app", status_code=301)

@app.get("/health")
def health():
    import shutil, subprocess as _sp
    def _resolve(name):
        p = shutil.which(name)
        if p: return p
        try:
            r = _sp.run(["which", name], capture_output=True, text=True, timeout=5)
            p = r.stdout.strip()
            if p: return p
        except Exception:
            pass
        hc = f"/usr/bin/{name}"
        return hc if os.path.isfile(hc) else None
    rb_path     = _resolve("rubberband")
    sox_path    = _resolve("sox")
    ffmpeg_path = _resolve("ffmpeg")
    engine      = "rubberband" if rb_path else ("sox" if sox_path else "none")
    print(f"[health] rubberband={rb_path}  sox={sox_path}  ffmpeg={ffmpeg_path}  engine={engine}", flush=True)
    return {
        "status":          "ok",
        "hostname":        os.environ.get("HOSTNAME", "unknown"),
        "engine":          engine,
        "rubberband":      rb_path is not None,
        "rubberband_path": rb_path,
        "sox":             sox_path is not None,
        "sox_path":        sox_path,
        "ffmpeg":          ffmpeg_path is not None,
        "ffmpeg_path":     ffmpeg_path,
    }

# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINT: /status — stato attuale del server (usato dal frontend pre-upload)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/status")
def server_status():
    """Frontend lo chiama prima del caricamento per avvisare l'utente se busy."""
    MAX_CONV = int(os.environ.get("MAX_CONCURRENT_CONVERSIONS", "3"))
    with _counters_lock:
        active_conv = _active_conversions
        active_ver  = _active_verifies
        active_ana  = _active_analyses

    # Stima durata media dalle ultime 50 conversioni ok
    recent = [e for e in list(_event_log)[-200:]
              if e["type"] in ("convert", "convert_sync") and e["esito"] == "ok"]
    avg_sec = (sum(e["duration_sec"] for e in recent[-50:]) / len(recent[-50:])) if recent else 60.0

    try:
        mem_pct = psutil.virtual_memory().percent
        cpu_pct = psutil.cpu_percent(interval=None)
    except Exception:
        mem_pct = cpu_pct = -1.0

    busy = active_conv >= MAX_CONV

    return {
        "busy":              busy,
        "active_conversions": active_conv,
        "active_verifies":   active_ver,
        "active_analyses":   active_ana,
        "max_conversions":   MAX_CONV,
        "queue_depth":       max(0, active_conv - MAX_CONV),
        "avg_duration_sec":  round(avg_sec, 1),
        "mem_pct":           mem_pct,
        "cpu_pct":           cpu_pct,
    }


@app.post("/verify")
async def verify(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    global _active_verifies
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Formato non supportato: {ext}")

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File troppo grande")
    file_mb = len(content) / 1_000_000

    tmp = str(TEMP_DIR / f"{uuid.uuid4()}{ext}")
    async with aiofiles.open(tmp, 'wb') as f:
        await f.write(content)

    with _counters_lock:
        _active_verifies += 1

    t0    = _time.time()
    esito = "ok"
    try:
        result = analyze_file(tmp)
        background_tasks.add_task(cleanup, tmp)
        if not result["success"]:
            esito = "error"
            raise HTTPException(500, result.get("error", "Errore"))
        return {
            **result,
            "filename": file.filename,
            "message":  "Certificato a 432 Hz ✅" if result["is_432"]
                        else "Questo brano è a 440 Hz — vuoi convertirlo?",
        }
    except HTTPException:
        esito = "error"
        raise
    except Exception as e:
        esito = "error"
        raise HTTPException(500, str(e))
    finally:
        with _counters_lock:
            _active_verifies -= 1
        _log_event(
            event_type="verify",
            esito=esito,
            duration_sec=_time.time() - t0,
            file_size_mb=file_mb,
            formato=ext.lstrip("."),
            request=request,
        )




# ── Synchronous conversion (no job state, no polling) ─────────────────────────

@app.post("/convert/sync")
async def convert_sync(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...), format: str = "mp3"):
    global _active_conversions
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Formato non supportato: {ext}")
    if format not in ["mp3", "m4a"]:
        format = "mp3"

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File troppo grande")
    file_mb = len(content) / 1_000_000

    uid     = str(uuid.uuid4())
    tmp_in  = str(TEMP_DIR / f"{uid}_in{ext}")
    tmp_out = str(TEMP_DIR / f"{uid}_432.{format}")
    with open(tmp_in, "wb") as fh:
        fh.write(content)

    with _counters_lock:
        _active_conversions += 1

    t0    = _time.time()
    esito = "ok"
    extra = {}
    try:
        import asyncio, functools
        loop = asyncio.get_event_loop()

        pre_check = await loop.run_in_executor(None, analyze_file, tmp_in)
        if pre_check.get("is_432"):
            esito = "already_432"
            return JSONResponse({
                "already_432":  True,
                "peak_freq":    pre_check["peak_freq"],
                "cents_vs_432": pre_check["cents_vs_432"],
                "message":      "Il brano è già a 432 Hz — nessuna conversione necessaria.",
            })

        from core.converter import _get_duration
        duration_sec = _get_duration(tmp_in) or 0.0
        sox_timeout  = max(120, int(duration_sec * 3))
        print(f"[convert/sync] duration={duration_sec:.1f}s  sox_timeout={sox_timeout}s", flush=True)

        result = await loop.run_in_executor(
            None,
            functools.partial(convert_to_432, tmp_in, tmp_out, max_seconds=360, sox_timeout=sox_timeout),
        )
        if not result["success"]:
            esito = "error"
            raise HTTPException(500, result.get("error", "Errore sconosciuto"))

        extra = {
            "shift_cents":       round(result.get("shift_applied", 0), 4),
            "post_cents_vs_432": round(result.get("post_cents_vs_432", 0), 4),
            "certified":         result.get("certified", False),
            "passes":            result.get("correction_passes", 1),
            "audio_duration_sec": round(duration_sec, 1),
        }

        stem    = Path(file.filename).stem
        exposed = "X-Pre-Freq,X-Shift-Cents,X-Post-Freq,X-Post-Cents,X-Certified,X-Correction-Passes"
        stats_headers = {
            "X-Pre-Freq":          str(round(result.get("pre_freq", 0), 4)),
            "X-Shift-Cents":       str(round(result.get("shift_applied", 0), 4)),
            "X-Post-Freq":         str(round(result.get("post_freq", 0), 4)),
            "X-Post-Cents":        str(round(result.get("post_cents_vs_432", 0), 4)),
            "X-Certified":         str(result.get("certified", False)),
            "X-Correction-Passes": str(round(result.get("correction_passes", 1))),
            "Access-Control-Expose-Headers": exposed,
        }
        if result.get("corr_pass_error"):
            stats_headers["X-Corr-Pass-Error"] = result["corr_pass_error"][:200]
            stats_headers["Access-Control-Expose-Headers"] += ",X-Corr-Pass-Error"

        background_tasks.add_task(cleanup, tmp_out)
        return FileResponse(
            path=tmp_out,
            filename=f"{stem}_432.{format}",
            media_type="audio/mpeg" if format == "mp3" else "audio/mp4",
            headers=stats_headers,
        )
    except HTTPException:
        esito = "error"
        raise
    except Exception as e:
        import traceback
        print(f"[convert/sync] Exception:\n{traceback.format_exc()}", flush=True)
        esito = "error"
        raise HTTPException(500, str(e))
    finally:
        with _counters_lock:
            _active_conversions -= 1
        if os.path.exists(tmp_in):
            try: os.remove(tmp_in)
            except Exception: pass
        _log_event(
            event_type="convert_sync",
            esito=esito,
            duration_sec=_time.time() - t0,
            file_size_mb=file_mb,
            formato=ext.lstrip("."),
            request=request,
            extra=extra,
        )


# ── Conversione asincrona (job-based) ─────────────────────────────────────────

@app.post("/convert-from-verify")
async def convert_from_verify(request: Request):
    """Avvia conversione riusando il file già caricato durante la verifica."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Body JSON non valido"}, status_code=400)

    verify_job_id = body.get("verify_job_id", "")
    format = body.get("format", "mp3")
    if format not in ("mp3", "m4a"):
        format = "mp3"

    # Recupera il file dalla verifica
    with _jobs_lock:
        verify_job = _jobs.get(verify_job_id)
    if not verify_job:
        return JSONResponse({"error": "Job di verifica non trovato"}, status_code=404)

    tmp_in = verify_job.get("_tmp_path", "")
    if not tmp_in or not os.path.exists(tmp_in):
        return JSONResponse({"error": "File di verifica non più disponibile — ricarica il file"}, status_code=410)

    from core.converter import _get_duration
    duration_sec = _get_duration(tmp_in) or 0.0
    sox_timeout = max(120, int(duration_sec * 3))
    file_mb = os.path.getsize(tmp_in) / 1_000_000

    job_id = str(uuid.uuid4())
    ext = Path(tmp_in).suffix.lower()
    tmp_out = str(TEMP_DIR / f"{job_id}_432.{format}")
    filename = verify_job.get("_formato", "audio") + ext

    _save_job(job_id, {"status": "uploading", "progress": 0})
    print(f"[convert/from-verify] job={job_id}  verify_job={verify_job_id}  size={file_mb:.1f}MB  duration={duration_sec:.1f}s", flush=True)

    t = threading.Thread(target=_run_conversion,
                         args=(job_id, tmp_in, tmp_out, format, filename, file_mb, duration_sec, sox_timeout, False),
                         daemon=True)
    t.start()

    return JSONResponse({"job_id": job_id})

@app.post("/convert")
async def convert_start(request: Request, file: UploadFile = File(...), format: str = "mp3"):
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Formato non supportato: {ext}")
    if format not in ["mp3", "m4a"]:
        format = "mp3"

    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File troppo grande")
    file_mb = len(content) / 1_000_000

    job_id  = str(uuid.uuid4())
    tmp_in  = str(TEMP_DIR / f"{job_id}_in{ext}")
    tmp_out = str(TEMP_DIR / f"{job_id}_432.{format}")
    with open(tmp_in, "wb") as fh:
        fh.write(content)

    from core.converter import _get_duration
    duration_sec = _get_duration(tmp_in) or 0.0
    sox_timeout  = max(120, int(duration_sec * 3))

    _save_job(job_id, {"status": "uploading", "progress": 0})
    print(f"[convert/async] job={job_id}  file={file.filename}  size={file_mb:.1f}MB  duration={duration_sec:.1f}s", flush=True)

    # Lancia la conversione in un thread background
    t = threading.Thread(target=_run_conversion,
                         args=(job_id, tmp_in, tmp_out, format, file.filename, file_mb, duration_sec, sox_timeout),
                         daemon=True)
    t.start()

    return JSONResponse({"job_id": job_id})

@app.get("/convert/status/{job_id}")
def convert_status(job_id: str):
    job = _load_job(job_id)
    if not job:
        raise HTTPException(404, "Job non trovato")
    # Non esporre output_path al client
    safe = {k: v for k, v in job.items() if k != "output_path"}
    return JSONResponse(safe)

@app.get("/convert/download/{job_id}")
def convert_download(job_id: str):
    job = _load_job(job_id)
    if not job:
        raise HTTPException(404, "Job non trovato")
    if job.get("status") != "done" or job.get("already_432"):
        raise HTTPException(400, "File non disponibile")
    output_path = job.get("output_path", "")
    if not output_path or not os.path.exists(output_path):
        raise HTTPException(404, "File di output non trovato")

    fmt  = job.get("format", "mp3")
    stem = Path(job.get("filename", "rephase")).stem
    media = "audio/mpeg" if fmt == "mp3" else "audio/mp4"

    # Cleanup job + file dopo 60s
    def _cleanup_job():
        import time; time.sleep(60)
        if os.path.exists(output_path):
            try: os.remove(output_path)
            except Exception: pass
        jp = _job_path(job_id)
        if jp.exists():
            try: jp.unlink()
            except Exception: pass
    threading.Thread(target=_cleanup_job, daemon=True).start()

    return FileResponse(path=output_path, filename=f"{stem}_432.{fmt}", media_type=media)

import asyncio
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import secrets

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/app", response_class=HTMLResponse)
async def frontend():
    with open("static/index.html") as f:
        return f.read()

# ── Legal pages ──────────────────────────────────────────────────────────────

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_it():
    with open("static/privacy.html") as f:
        return f.read()

@app.get("/privacy/en", response_class=HTMLResponse)
async def privacy_en():
    with open("static/privacy_en.html") as f:
        return f.read()

@app.get("/terms", response_class=HTMLResponse)
async def terms_it():
    with open("static/terms.html") as f:
        return f.read()

@app.get("/terms/en", response_class=HTMLResponse)
async def terms_en():
    with open("static/terms_en.html") as f:
        return f.read()

# ── Admin ─────────────────────────────────────────────────────────────────────

_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_FIXED_COSTS    = float(os.environ.get("ADMIN_FIXED_COSTS_CHF", "0"))

def _check_admin(authorization: str = Header(default=None)):
    if not _ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    if not secrets.compare_digest(authorization[7:].encode(), _ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "Wrong password")

# ── Polling-based FFT analysis (replaces WebSocket) ───────────────────────────

@app.post("/analyze/start")
async def analyze_start(request: Request, file: UploadFile = File(...), full_analysis: str = Form(default="0")):
    global _active_analyses
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Formato non supportato: {ext}")
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File troppo grande")
    file_mb = len(content) / 1_000_000

    uid = str(uuid.uuid4())
    tmp = str(TEMP_DIR / f"{uid}{ext}")
    with open(tmp, "wb") as fh:
        fh.write(content)

    job_id  = str(uuid.uuid4())
    is_full = full_analysis in ("1", "true", "True")

    with _jobs_lock:
        _jobs[job_id] = {
            "status":     "running",
            "windows":    [],
            "result":     None,
            "error":      None,
            "created_at": _time.time(),
            # vigile metadata
            "_file_mb":   file_mb,
            "_formato":   ext.lstrip("."),
            "_t0":        _time.time(),
            "_request_ip": request.client.host if request.client else "",
            "_ua":         (request.headers.get("user-agent", "") or "")[:80],
        }

    with _counters_lock:
        _active_analyses += 1

    def _run():
        global _active_analyses
        import tempfile, os as _os
        from core.converter import (
            _load_as_wav, _load_as_wav_sampled, _is_large_file,
            _read_wav_samples, _measure_a4_streaming,
        )
        tmp_wav = tempfile.mktemp(suffix=".wav")
        esito   = "ok"
        try:
            if is_full:
                _load_as_wav(tmp, tmp_wav, channels=1, max_seconds=None)
            elif _is_large_file(tmp):
                _load_as_wav_sampled(tmp, tmp_wav, channels=1)
                with _jobs_lock:
                    _jobs[job_id]["sampled"] = True
            else:
                _load_as_wav(tmp, tmp_wav, channels=1, max_seconds=90)
            samples, sr = _read_wav_samples(tmp_wav)
            for msg in _measure_a4_streaming(samples, sr):
                with _jobs_lock:
                    if msg.get("type") == "window":
                        _jobs[job_id]["windows"].append(msg)
                    elif msg.get("type") == "done":
                        _jobs[job_id]["result"] = msg["result"]
                        _jobs[job_id]["status"] = "done"
                    elif msg.get("type") == "error":
                        _jobs[job_id]["status"] = "error"
                        _jobs[job_id]["error"]  = msg.get("error", "Errore sconosciuto")
                        esito = "error"
        except Exception as e:
            esito = "error"
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"]  = str(e)
        finally:
            if _os.path.exists(tmp_wav): _os.remove(tmp_wav)
            # Conserva il file originale (tmp) per eventuale conversione successiva.
            # Verrà cancellato dopo 5 minuti se non usato.
            def _delayed_cleanup():
                import time; time.sleep(300)
                if _os.path.exists(tmp): _os.remove(tmp)
            threading.Thread(target=_delayed_cleanup, daemon=True).start()
            with _jobs_lock:
                _jobs[job_id]["_tmp_path"] = tmp
            with _jobs_lock:
                if _jobs[job_id]["status"] == "running":
                    _jobs[job_id]["status"] = "error"
                    _jobs[job_id]["error"]  = "Analisi interrotta"
                    esito = "error"
                meta = _jobs[job_id]

            with _counters_lock:
                _active_analyses -= 1

            # Log vigile dal thread
            duration = _time.time() - meta["_t0"]

            class _FakeRequest:
                class client:
                    host = meta["_request_ip"]
                class headers:
                    @staticmethod
                    def get(k, d=""): return meta["_ua"] if k == "user-agent" else d

            _log_event(
                event_type="analyze",
                esito=esito,
                duration_sec=duration,
                file_size_mb=meta["_file_mb"],
                formato=meta["_formato"],
                request=_FakeRequest(),
                extra={"full": is_full},
            )

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"job_id": job_id})


@app.get("/analyze/status/{job_id}")
async def analyze_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job non trovato o scaduto")
        return JSONResponse({
            "status":  job["status"],
            "windows": job["windows"],
            "result":  job["result"],
            "error":   job["error"],
            "sampled": job.get("sampled", False),
        })


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("static/admin.html") as f:
        return f.read()

@app.get("/admin/metrics")
async def admin_metrics(_: None = Depends(_check_admin)):
    from core.stripe_metrics import get_metrics
    from core.costs import load_costs, total_monthly_chf
    costs_data = load_costs()
    fixed = total_monthly_chf(costs_data) or _FIXED_COSTS
    data  = get_metrics(fixed_costs_chf=fixed, costs_data=costs_data)
    if "error" in data:
        raise HTTPException(502, data["error"])
    return JSONResponse(data)

@app.get("/admin/costs")
async def get_costs(_: None = Depends(_check_admin)):
    from core.costs import load_costs
    return JSONResponse(load_costs())

@app.post("/admin/costs")
async def post_costs(payload: dict, _: None = Depends(_check_admin)):
    from core.costs import save_costs, load_costs
    import uuid as _uuid
    costs_data = load_costs()
    if "launch_date" in payload:
        costs_data["launch_date"] = payload["launch_date"]
    if "items" in payload:
        for item in payload["items"]:
            if not item.get("id"):
                item["id"] = str(_uuid.uuid4())[:8]
        costs_data["items"] = payload["items"]
    if "phases" in payload:
        costs_data["phases"] = payload["phases"]
    save_costs(costs_data)
    from core.stripe_metrics import _cache
    _cache["ts"] = 0.0
    return JSONResponse({"ok": True})

# ══════════════════════════════════════════════════════════════════════════════
# 🚦 VIGILE URBANO — endpoint admin/log
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/admin/log")
async def admin_log(last: int = 100, _: None = Depends(_check_admin)):
    """
    Restituisce gli ultimi N eventi con statistiche aggregate.
    Chiamata: GET /admin/log?last=100
              Authorization: Bearer <ADMIN_PASSWORD>
    """
    events = list(_event_log)[-last:]

    today_str = datetime.now(timezone.utc).date().isoformat()
    oggi = [e for e in list(_event_log) if e["ts"].startswith(today_str)]

    # Statistiche per tipo
    def _stats(tipo):
        subset = [e for e in oggi if e["type"] == tipo]
        ok     = [e for e in subset if e["esito"] == "ok"]
        durate = [e["duration_sec"] for e in ok]
        return {
            "totale":       len(subset),
            "ok":           len(ok),
            "errori":       len([e for e in subset if e["esito"] == "error"]),
            "already_432":  len([e for e in subset if e["esito"] == "already_432"]),
            "durata_media": round(sum(durate) / len(durate), 1) if durate else 0,
            "durata_max":   round(max(durate), 1) if durate else 0,
        }

    try:
        mem_pct = psutil.virtual_memory().percent
        cpu_pct = psutil.cpu_percent(interval=None)
    except Exception:
        mem_pct = cpu_pct = -1.0

    with _counters_lock:
        active_now = {
            "conversions": _active_conversions,
            "verifies":    _active_verifies,
            "analyses":    _active_analyses,
        }

    return JSONResponse({
        "active_now": active_now,
        "mem_pct":    mem_pct,
        "cpu_pct":    cpu_pct,
        "oggi": {
            "verify":        _stats("verify"),
            "convert":       _stats("convert"),
            "convert_sync":  _stats("convert_sync"),
            "analyze":       _stats("analyze"),
        },
        "events": events,
    })


# ── Certification / Blockchain ─────────────────────────────────────────────

@app.post("/certify")
def certify_report(payload: dict):
    """Compute SHA-256 of the report JSON and anchor it on Bitcoin via OriginStamp."""
    import hashlib, json as _j, urllib.request, urllib.error

    report_str  = _j.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    report_hash = hashlib.sha256(report_str.encode("utf-8")).hexdigest()

    api_key = os.environ.get("ORIGINSTAMP_API_KEY", "")
    if not api_key:
        return JSONResponse({"report_hash": report_hash, "originstamp": None,
                             "warning": "ORIGINSTAMP_API_KEY non configurato — hash calcolato localmente."})

    body = _j.dumps({
        "hash":    report_hash,
        "comment": f"Rephase: {payload.get('file', {}).get('name', 'audio')}",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.originstamp.com/v4/timestamp/create",
        data=body,
        headers={"Authorization": f"Token {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            os_data = _j.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"OriginStamp HTTP {e.code}: {e.reason}")
    except Exception as e:
        raise HTTPException(502, f"OriginStamp error: {e}")

    return JSONResponse({"report_hash": report_hash, "originstamp": os_data})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True, timeout_keep_alive=600)


# ── Stripe Checkout ────────────────────────────────────────────────────────

# ── Auth OTP ──────────────────────────────────────────────────────────────────

@app.post("/validate-email")
async def validate_email_endpoint(request: Request):
    """Valida un indirizzo email — blocca servizi di email temporanea."""
    from core.email_validator import validate_email
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"valid": False, "error": "Body JSON non valido"}, status_code=400)
    email = body.get("email", "")
    result = validate_email(email)
    status = 200 if result["valid"] else 400
    return JSONResponse(result, status_code=status)

@app.post("/auth/send-otp")
async def send_otp_endpoint(request: Request):
    """Invia codice OTP a 6 cifre via email (Resend)."""
    from core.auth import generate_otp
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Body JSON non valido"}, status_code=400)
    email = body.get("email", "")
    if not email:
        return JSONResponse({"success": False, "error": "Email richiesta"}, status_code=400)
    result = generate_otp(email)
    status = 200 if result["success"] else 400
    return JSONResponse(result, status_code=status)

@app.post("/auth/verify-otp")
async def verify_otp_endpoint(request: Request):
    """Verifica codice OTP e crea sessione."""
    from core.auth import verify_otp
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "Body JSON non valido"}, status_code=400)
    email = body.get("email", "")
    code  = body.get("code", "")
    if not email or not code:
        return JSONResponse({"success": False, "error": "Email e codice richiesti"}, status_code=400)
    result = verify_otp(email, code)
    status = 200 if result["success"] else 400
    return JSONResponse(result, status_code=status)

# ── Stripe Checkout ───────────────────────────────────────────────────────────

@app.post("/create-checkout-session")
async def create_checkout_session(request: Request):
    """Crea una sessione Stripe Checkout per abbonamento mensile o annuale."""
    print("[checkout] route chiamata", flush=True)
    try:
        body = await request.json()
        print(f"[checkout] body={body}", flush=True)
    except Exception as e:
        print(f"[checkout] body parse error: {e}", flush=True)
        return JSONResponse({"error": "Body JSON non valido"}, status_code=400)

    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    print(f"[checkout] stripe_key={'SET' if stripe.api_key else 'MISSING'}", flush=True)
    if not stripe.api_key:
        return JSONResponse({"error": "STRIPE_SECRET_KEY non configurata"}, status_code=500)

    plan = body.get("plan", "monthly")
    if plan == "annual":
        price_id = os.environ.get("STRIPE_PRICE_ANNUAL", "")
    elif plan == "lifetime":
        price_id = os.environ.get("STRIPE_PRICE_LIFETIME", "")
    else:
        price_id = os.environ.get("STRIPE_PRICE_MONTHLY", "")
    print(f"[checkout] plan={plan} price_id={'SET('+price_id[:12]+')' if price_id else 'MISSING'}", flush=True)

    if not price_id:
        return JSONResponse({"error": f"Price ID non configurato per piano: {plan}"}, status_code=400)

    base_url = os.environ.get("BASE_URL", "https://rephase-app.onrender.com")
    checkout_mode = "payment" if plan == "lifetime" else "subscription"

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode=checkout_mode,
            success_url=f"{base_url}/app?payment=success",
            cancel_url=f"{base_url}/app?payment=cancelled",
        )
        print(f"[checkout] session creata: {session.id}", flush=True)
        return JSONResponse({"url": session.url})
    except Exception as e:
        print(f"[checkout] ERRORE stripe: {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"error": f"Stripe error: {str(e)}"}, status_code=502)

