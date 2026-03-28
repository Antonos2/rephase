#!/usr/bin/env python3
import os, uuid
import aiofiles
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from core.converter import convert_to_432, analyze_file

app = FastAPI(title="Rephase API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TEMP_DIR = Path("/tmp/rephase")
TEMP_DIR.mkdir(exist_ok=True)
ALLOWED = {".mp3",".m4a",".wav",".flac",".aac",".aiff"}
MAX_SIZE = 200*1024*1024

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
    result = convert_to_432(tmp_in, tmp_out)
    background_tasks.add_task(cleanup, tmp_in)
    if not result["success"]: raise HTTPException(500, result.get("error","Errore"))
    background_tasks.add_task(cleanup, tmp_out)
    stem = Path(file.filename).stem
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
    return FileResponse(path=tmp_out, filename=f"{stem}_432.{format}", media_type="audio/mpeg" if format=="mp3" else "audio/mp4", headers=stats_headers)

from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/app", response_class=HTMLResponse)
async def frontend():
    with open("static/index.html") as f:
        return f.read()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
