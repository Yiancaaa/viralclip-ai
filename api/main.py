"""API FastAPI de VIRALCLIP AI (MVP)."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from viralclip.config import settings
from viralclip.connectors import validate_stream_url
from viralclip.db import SessionLocal, init_db
from viralclip.models import Clip, Job, LiveSession, Moment, Video
from viralclip.storage import LocalStorage, get_storage
from worker.classify import (BUTTONS, CATEGORIES, SUBCATEGORIES,
                             classification_to_record, classify_video,
                             effective_category)
from worker.expiry import expire_clips, time_remaining
from worker.live import live_manager

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # Limpieza de caducidad al arrancar (el scheduler horario vive en worker/scheduler.py)
    try:
        db = SessionLocal()
        expire_clips(db, get_storage())
        db.close()
    except Exception:
        pass
    yield


app = FastAPI(title="VIRALCLIP AI", version="0.1.0", lifespan=lifespan)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name, "version": "0.1.0",
            "storage_backend": settings.storage_backend,
            "whisper_model": settings.whisper_model}


# ---------------------------------------------------------------- streams
class ValidateRequest(BaseModel):
    url: str


@app.post("/api/streams/validate")
def validate_stream(body: ValidateRequest):
    return validate_stream_url(body.url, settings.twitch_client_id,
                               settings.twitch_client_secret, settings.youtube_api_key)


class StartRequest(BaseModel):
    url: str


@app.post("/api/streams/{stream_id}/start")
def start_stream(stream_id: int, db: Session = Depends(get_db)):
    s = db.query(LiveSession).filter(LiveSession.id == stream_id).one_or_none()
    if not s:
        raise HTTPException(404, "live session no encontrada")
    live_manager.set_status(stream_id, "live")
    s.status = "live"
    s.started_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": s.id, "status": s.status, "platform": s.platform, "channel": s.channel}


@app.post("/api/streams")
def create_stream(body: StartRequest, db: Session = Depends(get_db)):
    v = validate_stream_url(body.url, settings.twitch_client_id,
                            settings.twitch_client_secret, settings.youtube_api_key)
    if not v["valid"]:
        raise HTTPException(400, v["detail"])
    entry = live_manager.add(v["platform"], v["identifier"], body.url)
    s = LiveSession(platform=v["platform"], channel=v["identifier"], url=body.url,
                    status=entry.status, detail=v["detail"])
    db.add(s)
    db.commit()
    db.refresh(s)
    # Sincroniza el id del manager con el id de BD para /start y /stop
    live_manager._streams.pop(entry.id, None)
    live_manager._streams[s.id] = entry
    entry.id = s.id
    return {"id": s.id, "platform": s.platform, "channel": s.channel,
            "status": s.status, "live_status": v["live_status"], "detail": v["detail"]}


@app.post("/api/streams/{stream_id}/stop")
def stop_stream(stream_id: int, db: Session = Depends(get_db)):
    s = db.query(LiveSession).filter(LiveSession.id == stream_id).one_or_none()
    if not s:
        raise HTTPException(404, "live session no encontrada")
    live_manager.stop(stream_id)
    s.status = "stopped"
    s.ended_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": s.id, "status": s.status}


@app.get("/api/streams")
def list_streams(db: Session = Depends(get_db)):
    return [{"id": s.id, "platform": s.platform, "channel": s.channel, "url": s.url,
             "status": s.status, "detail": s.detail} for s in db.query(LiveSession).all()]


# ---------------------------------------------------------------- videos / jobs
@app.post("/api/videos/upload")
async def upload_video(file: UploadFile = File(...), title: str = "",
                       db: Session = Depends(get_db)):
    storage = get_storage()
    suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
    tmp = f"./data/tmp_upload{suffix}"
    Path("./data").mkdir(parents=True, exist_ok=True)
    data = await file.read()
    if len(data) > settings.max_upload_gb * 1024 ** 3:
        raise HTTPException(413, "Archivo mayor que MAX_UPLOAD_GB")
    Path(tmp).write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    dup = db.query(Video).filter(Video.sha256 == sha).first()
    if dup:
        os.remove(tmp)
        return {"id": dup.id, "duplicate_of": dup.id, "title": dup.title}
    key = f"originals/{sha[:12]}{suffix}"
    storage.save(key, tmp)
    os.remove(tmp)
    v = Video(title=title or file.filename, platform="upload", file_path=key,
              file_size_bytes=len(data), sha256=sha)
    db.add(v)
    db.commit()
    db.refresh(v)
    return {"id": v.id, "title": v.title, "file_path": key, "size_bytes": len(data)}


def _video_dict(v: Video) -> dict:
    category, source = effective_category(v)
    return {"id": v.id, "title": v.title, "platform": v.platform,
            "duration_seconds": v.duration_seconds, "file_path": v.file_path,
            "category": v.category or "", "subcategory": v.subcategory or "",
            "classification_confidence": v.classification_confidence or 0.0,
            "classification_source": v.classification_source or "",
            "manual_override": bool(v.manual_override),
            "effective_category": category, "effective_category_source": source}


@app.get("/api/videos/{video_id}")
def get_video(video_id: int, db: Session = Depends(get_db)):
    v = db.query(Video).filter(Video.id == video_id).one_or_none()
    if not v:
        raise HTTPException(404, "video no encontrado")
    return _video_dict(v)


@app.post("/api/videos/{video_id}/classify")
def classify_video_endpoint(video_id: int, db: Session = Depends(get_db)):
    """Clasificación automática del tipo de directo (muestras de audio y
    vídeo, modelo whisper tiny). Guarda category/subcategory/confidence
    en el vídeo. Si el usuario ya corrigió manualmente la categoría
    (manual_override), la automática NO la sobrescribe: se devuelve la
    clasificación automática calculada pero se conserva la manual."""
    v = db.query(Video).filter(Video.id == video_id).one_or_none()
    if not v:
        raise HTTPException(404, "video no encontrado")
    storage = get_storage()
    local_src = None
    tmp_path = None
    if isinstance(storage, LocalStorage):
        local_src = str(Path(storage.root) / v.file_path)
        if not Path(local_src).exists() and os.path.exists(v.file_path):
            local_src = v.file_path
    else:
        tmp_path = f"./data/classify_{v.id}.mp4"
        Path(tmp_path).write_bytes(storage.read(v.file_path))
        local_src = tmp_path
    if not local_src or not os.path.exists(local_src):
        raise HTTPException(404, "archivo de vídeo no encontrado en almacenamiento")
    try:
        result = classify_video(local_src)
    except Exception as e:
        raise HTTPException(500, f"clasificación fallida: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
    if not v.manual_override:
        rec = classification_to_record(result)
        v.category = rec["category"]
        v.subcategory = rec["subcategory"]
        v.classification_confidence = rec["classification_confidence"]
        v.classification_source = rec["classification_source"]
        v.manual_override = False
        v.classification_signals = rec["classification_signals"]
        db.commit()
        db.refresh(v)
    return {"video_id": v.id, **result,
            "saved": not v.manual_override,
            "manual_override_kept": bool(v.manual_override),
            "stored": _video_dict(v)}


class CategoryCorrection(BaseModel):
    category: str
    subcategory: str = ""


@app.patch("/api/videos/{video_id}/category")
def correct_video_category(video_id: int, body: CategoryCorrection,
                           db: Session = Depends(get_db)):
    """Corrección manual de la categoría SIN reintroducir la URL. La
    selección manual tiene prioridad sobre la automática (manual_override
    =True) y el proceso posterior usa esta categoría para puntuar y
    elegir el layout. `subcategory` opcional, debe pertenecer a la
    categoría elegida."""
    v = db.query(Video).filter(Video.id == video_id).one_or_none()
    if not v:
        raise HTTPException(404, "video no encontrado")
    if body.category not in CATEGORIES:
        raise HTTPException(400, f"category inválida; valores: {CATEGORIES}")
    sub = body.subcategory or SUBCATEGORIES[body.category][0]
    if sub not in SUBCATEGORIES[body.category]:
        raise HTTPException(
            400, f"subcategory '{sub}' no pertenece a '{body.category}'; "
                 f"valores: {SUBCATEGORIES[body.category]}")
    v.category = body.category
    v.subcategory = sub
    v.classification_confidence = 1.0
    v.classification_source = "manual"
    v.manual_override = True
    db.commit()
    db.refresh(v)
    return {"video_id": v.id, **_video_dict(v),
            "dashboard_button": BUTTONS.get(body.category)}


@app.post("/api/videos/{video_id}/process")
def process_video(video_id: int, db: Session = Depends(get_db)):
    v = db.query(Video).filter(Video.id == video_id).one_or_none()
    if not v:
        raise HTTPException(404, "video no encontrado")
    job = Job(video_id=v.id, kind="process", status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    # Ejecución síncrona en este MVP (el worker encolado es fase posterior);
    # el endpoint devuelve el resultado real del pipeline.
    from worker.jobs import process_job

    try:
        result = process_job(db, job.id, get_storage())
    except Exception as e:
        raise HTTPException(500, f"procesamiento fallido: {e}")
    return result


@app.get("/api/jobs")
def list_jobs(db: Session = Depends(get_db)):
    return [{"id": j.id, "video_id": j.video_id, "kind": j.kind, "status": j.status,
             "error": j.error, "created_at": j.created_at.isoformat() if j.created_at else None}
            for j in db.query(Job).order_by(Job.id.desc()).all()]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    j = db.query(Job).filter(Job.id == job_id).one_or_none()
    if not j:
        raise HTTPException(404, "job no encontrado")
    return {"id": j.id, "video_id": j.video_id, "kind": j.kind, "status": j.status,
            "error": j.error,
            "moments": [{"id": m.id, "start": m.start_seconds, "end": m.end_seconds,
                         "score": m.score, "text": m.transcript_text,
                         "titles": json.loads(m.titles or "[]")} for m in j.moments]}


@app.get("/api/moments")
def list_moments(min_score: float = 0.0, db: Session = Depends(get_db)):
    return [{"id": m.id, "job_id": m.job_id, "video_id": m.video_id,
             "start": m.start_seconds, "end": m.end_seconds, "score": m.score,
             "text": m.transcript_text, "titles": json.loads(m.titles or "[]"),
             "breakdown": json.loads(m.score_breakdown or "{}")}
            for m in db.query(Moment).filter(Moment.score >= min_score).order_by(Moment.score.desc()).all()]


# ---------------------------------------------------------------- clips / library
def _clip_dict(c: Clip) -> dict:
    tr = time_remaining(c)
    return {"id": c.id, "video_id": c.video_id, "title": c.title, "platform": c.platform,
            "duration_seconds": c.duration_seconds, "score": c.score,
            "favorite": c.favorite, "expired": c.expired,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "expires_at": tr["expires_at"], "seconds_remaining": tr["seconds_remaining"],
            "file_size_bytes": c.file_size_bytes}


@app.get("/api/clips")
def list_clips(db: Session = Depends(get_db)):
    return [_clip_dict(c) for c in db.query(Clip).order_by(Clip.score.desc()).all()]


@app.get("/api/library")
def library(q: str = "", platform: str = "", min_score: float = 0.0,
            favorites_only: bool = False, min_duration: float = 0.0,
            max_duration: float = 0.0, date_from: str = "", date_to: str = "",
            db: Session = Depends(get_db)):
    query = db.query(Clip)
    if q:
        query = query.filter(Clip.title.contains(q))
    if platform:
        query = query.filter(Clip.platform == platform)
    if min_score:
        query = query.filter(Clip.score >= min_score)
    if favorites_only:
        query = query.filter(Clip.favorite.is_(True))
    if min_duration:
        query = query.filter(Clip.duration_seconds >= min_duration)
    if max_duration:
        query = query.filter(Clip.duration_seconds <= max_duration)
    if date_from:
        query = query.filter(Clip.created_at >= datetime.fromisoformat(date_from))
    if date_to:
        query = query.filter(Clip.created_at <= datetime.fromisoformat(date_to))
    return [_clip_dict(c) for c in query.order_by(Clip.created_at.desc()).all()]


@app.post("/api/clips/{clip_id}/favorite")
def favorite_clip(clip_id: int, db: Session = Depends(get_db)):
    c = db.query(Clip).filter(Clip.id == clip_id).one_or_none()
    if not c:
        raise HTTPException(404, "clip no encontrado")
    c.favorite = not c.favorite
    db.commit()
    return {"id": c.id, "favorite": c.favorite}


@app.delete("/api/clips/{clip_id}")
def delete_clip(clip_id: int, db: Session = Depends(get_db)):
    c = db.query(Clip).filter(Clip.id == clip_id).one_or_none()
    if not c:
        raise HTTPException(404, "clip no encontrado")
    storage = get_storage()
    freed = 0
    for key in (c.file_path, c.thumbnail_path):
        if key:
            freed += storage.size_bytes(key)
            storage.delete(key)
    db.delete(c)
    db.commit()
    return {"id": clip_id, "deleted": True, "freed_bytes": freed}


@app.get("/api/clips/{clip_id}/download")
def download_clip(clip_id: int, db: Session = Depends(get_db)):
    c = db.query(Clip).filter(Clip.id == clip_id).one_or_none()
    if not c:
        raise HTTPException(404, "clip no encontrado")
    if c.expired or not c.file_path:
        raise HTTPException(410, "clip caducado: el archivo fue eliminado, solo quedan los metadatos")
    storage = get_storage()
    if isinstance(storage, LocalStorage):
        path = Path(storage.root) / c.file_path
        if not path.exists():
            raise HTTPException(404, "archivo no encontrado en almacenamiento")
        return FileResponse(path, media_type="video/mp4", filename=f"viralclip_{c.id}.mp4")
    # S3: descarga temporal y sirve el archivo
    tmp = f"./data/dl_{c.id}.mp4"
    Path(tmp).write_bytes(storage.read(c.file_path))
    return FileResponse(tmp, media_type="video/mp4", filename=f"viralclip_{c.id}.mp4")


@app.get("/api/clips/{clip_id}/thumbnail")
def thumbnail_clip(clip_id: int, db: Session = Depends(get_db)):
    """Sirve la miniatura JPG del clip (`thumbs/{id}.jpg`, generada por
    el worker en `process_job`). 404 si el clip no existe o si la
    miniatura no está en almacenamiento; 410 si el clip caducó (igual
    que /download) o si no tiene miniatura registrada."""
    c = db.query(Clip).filter(Clip.id == clip_id).one_or_none()
    if not c:
        raise HTTPException(404, "clip no encontrado")
    if c.expired or not c.thumbnail_path:
        raise HTTPException(410, "miniatura no disponible: clip caducado o sin miniatura")
    storage = get_storage()
    if isinstance(storage, LocalStorage):
        path = Path(storage.root) / c.thumbnail_path
        if not path.exists():
            raise HTTPException(404, "miniatura no encontrada en almacenamiento")
        return FileResponse(path, media_type="image/jpeg", filename=f"viralclip_{c.id}.jpg")
    # S3: descarga temporal y sirve el archivo
    tmp = f"./data/dl_{c.id}_thumb.jpg"
    Path(tmp).write_bytes(storage.read(c.thumbnail_path))
    return FileResponse(tmp, media_type="image/jpeg", filename=f"viralclip_{c.id}.jpg")


@app.get("/api/storage/quota")
def storage_quota(db: Session = Depends(get_db)):
    storage = get_storage()
    if isinstance(storage, LocalStorage):
        used = storage.total_used_bytes()
    else:
        used = sum(c.file_size_bytes for c in db.query(Clip).filter(Clip.expired.is_(False)).all())
    limit = int(settings.storage_quota_gb * 1024 ** 3)
    return {"used_bytes": used, "limit_bytes": limit, "limit_gb": settings.storage_quota_gb,
            "available_bytes": max(0, limit - used),
            "used_percent": round(100 * used / limit, 2) if limit else 0.0,
            "retention_days": settings.clip_retention_days,
            "backend": settings.storage_backend}


# ---------------------------------------------------------------- dashboard
# Dashboard estático (app/static/): se monta DESPUÉS de todas las rutas
# /api/* para no sombrear ningún endpoint existente. StaticFiles con
# html=True sirve index.html en /.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="dashboard")
