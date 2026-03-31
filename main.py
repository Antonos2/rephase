#!/usr/bin/env python3
import os, uuid, threading
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

@app.get("/")
def root():
    return {"name":"Rephase API","version":"1.0.0","motto":"Out of phase? Get Rephase.","status":"online"}

@app.get("/health")
def health():
    import shutil, subprocess as _sp
    sox_which    = shutil.which("sox")
    ffmpeg_which = shutil.which("ffmpeg")
    def _shell_which(cmd):
        try:
            r = _sp.run(["which", cmd], capture_output=True, text=True, timeout=5)
            return r.stdout.strip() or None
        except Exception:
            return None
    sox_path    = sox_which    or _shell_which("sox")
    ffmpeg_path = ffmpeg_which or _shell_which("ffmpeg")
    print(f"[health] sox={sox_path}  ffmpeg={ffmpeg_path}", flush=True)
    return {
        "status":      "ok",
        "sox":         sox_path is not None,
        "sox_path":    sox_path,
        "ffmpeg":      ffmpeg_path is not None,
        "ffmpeg_path": ffmpeg_path,
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


@app.post("/convert")
async def convert(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...), format: str = "mp3"):
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
    async with aiofiles.open(tmp_in, 'wb') as f:
        await f.write(content)

    with _counters_lock:
        _active_conversions += 1

    t0    = _time.time()
    esito = "ok"
    extra = {}
    try:
        analysis = analyze_file(tmp_in)
        if analysis.get("is_432"):
            background_tasks.add_task(cleanup, tmp_in)
            esito = "already_432"
            return JSONResponse({
                "already_432":  True,
                "peak_freq":    analysis["peak_freq"],
                "cents_vs_432": analysis["cents_vs_432"],
                "message":      "Il brano è già a 432 Hz — nessuna conversione necessaria.",
            })
        result = convert_to_432(tmp_in, tmp_out)
        background_tasks.add_task(cleanup, tmp_in)
        if not result["success"]:
            esito = "error"
            raise HTTPException(500, result.get("error", "Errore"))

        extra = {
            "shift_cents":       round(result.get("shift_applied", 0), 4),
            "post_cents_vs_432": round(result.get("post_cents_vs_432", 0), 4),
            "certified":         result.get("certified", False),
            "passes":            result.get("correction_passes", 1),
        }

        background_tasks.add_task(cleanup, tmp_out)
        stem    = Path(file.filename).stem
        exposed = "X-Pre-Freq,X-Shift-Cents,X-Post-Freq,X-Post-Cents,X-Certified,X-Correction-Passes"
        stats_headers = {
            "X-Pre-Freq":          str(round(result.get("pre_freq", 0), 4)),
            "X-Shift-Cents":       str(round(result.get("shift_applied", 0), 4)),
            "X-Post-Freq":         str(round(result.get("post_freq", 0), 4)),
            "X-Post-Cents":        str(round(result.get("post_cents_vs_432", 0), 4)),
            "X-Certified":         str(result.get("certified", False)),
            "X-Correction-Passes": str(result.get("correction_passes", 1)),
            "Access-Control-Expose-Headers": exposed,
        }
        if result.get("corr_pass_error"):
            stats_headers["X-Corr-Pass-Error"] = result["corr_pass_error"][:200]
            stats_headers["Access-Control-Expose-Headers"] += ",X-Corr-Pass-Error"
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
        esito = "error"
        raise HTTPException(500, str(e))
    finally:
        with _counters_lock:
            _active_conversions -= 1
        _log_event(
            event_type="convert",
            esito=esito,
            duration_sec=_time.time() - t0,
            file_size_mb=file_mb,
            formato=ext.lstrip("."),
            request=request,
            extra=extra,
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
            functools.partial(convert_to_432, tmp_in, tmp_out, sox_timeout=sox_timeout),
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


import asyncio
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import secrets

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/app", response_class=HTMLResponse)
async def frontend():
    with open("static/index.html") as f:
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
            for p in [tmp_wav, tmp]:
                if _os.path.exists(p): _os.remove(p)
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
