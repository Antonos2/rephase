#!/usr/bin/env python3
import os, uuid, threading
import time as _time
import aiofiles
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Depends, Header, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from core.converter import convert_to_432, analyze_file

# ── In-memory job store (polling-based analysis) ──────────────────────────────
_jobs: dict = {}
_jobs_lock = threading.Lock()

def _cleanup_jobs():
    """Background thread: remove jobs older than 10 minutes."""
    while True:
        _time.sleep(300)
        cutoff = _time.time() - 600
        with _jobs_lock:
            expired = [k for k, v in _jobs.items() if v.get("created_at", 0) < cutoff]
            for k in expired:
                del _jobs[k]

threading.Thread(target=_cleanup_jobs, daemon=True).start()

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
    import shutil
    return {"status":"ok","sox":shutil.which("sox") is not None,"ffmpeg":shutil.which("ffmpeg") is not None}

@app.post("/verify")
async def verify(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED: raise HTTPException(400, f"Formato non supportato: {ext}")
    tmp = str(TEMP_DIR/f"{uuid.uuid4()}{ext}")
    async with aiofiles.open(tmp,'wb') as f:
        content = await file.read()
        if len(content)>MAX_SIZE: raise HTTPException(413,"File troppo grande")
        await f.write(content)
    result = analyze_file(tmp)
    background_tasks.add_task(cleanup, tmp)
    if not result["success"]: raise HTTPException(500, result.get("error","Errore"))
    return {**result,"filename":file.filename,"message":"Certificato a 432 Hz ✅" if result["is_432"] else "Questo brano è a 440 Hz — vuoi convertirlo?"}

@app.post("/convert")
async def convert(background_tasks: BackgroundTasks, file: UploadFile = File(...), format: str = "mp3"):
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED: raise HTTPException(400, f"Formato non supportato: {ext}")
    if format not in ["mp3","m4a"]: format="mp3"
    uid = str(uuid.uuid4())
    tmp_in  = str(TEMP_DIR/f"{uid}_in{ext}")
    tmp_out = str(TEMP_DIR/f"{uid}_432.{format}")
    async with aiofiles.open(tmp_in,'wb') as f:
        content = await file.read()
        if len(content)>MAX_SIZE: raise HTTPException(413,"File troppo grande")
        await f.write(content)
    analysis = analyze_file(tmp_in)
    if analysis.get("is_432"):
        background_tasks.add_task(cleanup, tmp_in)
        return JSONResponse({"already_432": True, "peak_freq": analysis["peak_freq"], "cents_vs_432": analysis["cents_vs_432"], "message": "Il brano \u00e8 gi\u00e0 a 432 Hz \u2014 nessuna conversione necessaria."})
    result = convert_to_432(tmp_in, tmp_out, max_seconds=90)
    background_tasks.add_task(cleanup, tmp_in)
    if not result["success"]: raise HTTPException(500, result.get("error","Errore"))
    background_tasks.add_task(cleanup, tmp_out)
    stem = Path(file.filename).stem
    exposed = "X-Pre-Freq,X-Shift-Cents,X-Post-Freq,X-Post-Cents,X-Certified,X-Correction-Passes,X-Trimmed-Seconds"
    stats_headers = {
        "X-Trimmed-Seconds": "90",
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
    return FileResponse(path=tmp_out, filename=f"{stem}_432.{format}", media_type="audio/mpeg" if format=="mp3" else "audio/mp4", headers=stats_headers)

import asyncio, json as _json
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
async def analyze_start(file: UploadFile = File(...), full_analysis: str = Form(default="0")):
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Formato non supportato: {ext}")
    content = await file.read()
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File troppo grande")

    uid = str(uuid.uuid4())
    tmp = str(TEMP_DIR / f"{uid}{ext}")
    with open(tmp, "wb") as fh:
        fh.write(content)

    job_id   = str(uuid.uuid4())
    is_full  = full_analysis in ("1", "true", "True")

    with _jobs_lock:
        _jobs[job_id] = {
            "status":     "running",
            "windows":    [],
            "result":     None,
            "error":      None,
            "created_at": _time.time(),
        }

    def _run():
        import tempfile, os as _os
        from core.converter import (
            _load_as_wav, _load_as_wav_sampled, _is_large_file,
            _read_wav_samples, _measure_a4_streaming,
        )
        tmp_wav = tempfile.mktemp(suffix=".wav")
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
        except Exception as e:
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
    data = get_metrics(fixed_costs_chf=fixed, costs_data=costs_data)
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
    # Merge: replace items and phases; preserve launch_date if not sent
    if "launch_date" in payload:
        costs_data["launch_date"] = payload["launch_date"]
    if "items" in payload:
        # Assign id to new items that don't have one
        for item in payload["items"]:
            if not item.get("id"):
                item["id"] = str(_uuid.uuid4())[:8]
        costs_data["items"] = payload["items"]
    if "phases" in payload:
        costs_data["phases"] = payload["phases"]
    save_costs(costs_data)
    from core.stripe_metrics import _cache
    _cache["ts"] = 0.0  # invalidate metrics cache
    return JSONResponse({"ok": True})

# ── Certification / Blockchain ─────────────────────────────────────────────

@app.post("/certify")
def certify_report(payload: dict):
    """Compute SHA-256 of the report JSON and anchor it on Bitcoin via OriginStamp."""
    import hashlib, json as _j, urllib.request, urllib.error

    # Canonical JSON (sorted keys, no spaces) → deterministic hash
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
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
