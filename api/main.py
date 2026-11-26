"""API FastAPI de VIRALCLIP AI (MVP)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
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
from worker.download import DownloadError, download_video_url, is_youtube_video_url
from worker.upload import (PRESIGNED_URL_EXPIRES_SECONDS,
                           UploadTooLargeError, build_upload_key,
                           presign_direct_upload, register_uploaded_video,
                           validate_upload_request)
from worker.classify import (BUTTONS, CATEGORIES, SUBCATEGORIES,
                             classification_to_record, classify_video,
                             effective_category)
from worker.expiry import expire_clips, time_remaining
from worker.live import live_manager

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # El sistema de archivos del contenedor es efímero: tras cada
    # reinicio o despliegue ./data no existe. Crearlo al arrancar cubre
    # todos los temporales (subidas, cookies, descargas, miniaturas);
    # sin esto, escribir en ./data da FileNotFoundError -> HTTP 500
    # (pasó con la subida de cookies en vivo el 2026-11-26).
    Path("./data").mkdir(parents=True, exist_ok=True)
    # Limpieza de caducidad al arrancar (el scheduler horario vive en worker/scheduler.py)
    try:
        db = SessionLocal()
        expire_clips(db, get_storage())
        # Trabajos que quedaron a medias por un reinicio o un despliegue:
        # no se reanudan solos; se marcan como error para no dejar un
        # "processing" eterno en el panel.
        stale = db.query(Job).filter(Job.status.in_(["queued", "processing"])).all()
        for j in stale:
            j.status = "error"
            j.error = ("Interrumpido por el reinicio del servidor; "
                       "vuelve a procesar el vídeo.")
        db.commit()
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


# ---------------------------------------------------------------- subida directa a S3 (URL prefirmada)
class PresignRequest(BaseModel):
    filename: str
    size_bytes: int
    sha256: str
    title: str = ""
    duration_seconds: float = 0.0
    content_type: str = "video/mp4"


@app.post("/api/videos/upload/presign")
def presign_upload(body: PresignRequest, db: Session = Depends(get_db)):
    """URL prefirmada para subir el archivo DIRECTO a S3 (PUT).

    El navegador sube a esa URL sin pasar por el proceso web: el
    endpoint clásico lee el archivo entero en RAM y pasa por el proxy
    de Render, y una subida de un vídeo de Studio llevaba 1 hora sin
    llegar nunca (verificado 2026-11-26). Valida tamaño, SHA-256,
    extensión, duplicado y cuota ANTES de devolver la URL. Si el vídeo
    ya existe (mismo SHA-256) responde duplicate y no hace falta subir
    nada. 501 si el backend no soporta URLs prefirmadas (local): el
    panel usa entonces la subida clásica como reserva.
    """
    max_size = int(settings.max_upload_gb * 1024 ** 3)
    try:
        suffix = validate_upload_request(
            body.filename, body.size_bytes, body.sha256,
            body.duration_seconds,
            max_size_bytes=max_size,
            max_duration_seconds=settings.max_download_duration_seconds)
    except UploadTooLargeError as e:
        raise HTTPException(413, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    dup = db.query(Video).filter(Video.sha256 == body.sha256).first()
    if dup:
        return {"duplicate": True, "duplicate_of": dup.id, "video_id": dup.id,
                "title": dup.title}
    storage = get_storage()
    if isinstance(storage, LocalStorage):
        used = storage.total_used_bytes()
    else:
        used = sum(c.file_size_bytes for c in
                   db.query(Clip).filter(Clip.expired.is_(False)).all())
    limit = int(settings.storage_quota_gb * 1024 ** 3)
    if used + body.size_bytes > limit:
        raise HTTPException(
            413, f"Cuota de almacenamiento llena: {used / 1024 ** 3:.2f} GB "
                 f"usados de {settings.storage_quota_gb:.0f} GB; el archivo "
                 f"necesita {body.size_bytes / 1024 ** 2:.0f} MB más.")
    key = build_upload_key(body.sha256, suffix)
    try:
        url = presign_direct_upload(
            storage, key, content_type=body.content_type or "video/mp4")
    except NotImplementedError as e:
        raise HTTPException(501, str(e))
    return {"upload_url": url, "key": key,
            "expires_in": PRESIGNED_URL_EXPIRES_SECONDS,
            "content_type": body.content_type or "video/mp4",
            "max_upload_gb": settings.max_upload_gb}


class CompleteUploadRequest(BaseModel):
    key: str
    sha256: str
    filename: str
    title: str = ""
    size_bytes: int
    duration_seconds: float = 0.0


@app.post("/api/videos/upload/complete")
def complete_upload(body: CompleteUploadRequest, db: Session = Depends(get_db)):
    """Registra en BD un vídeo ya subido directo a S3.

    Verifica que la clave es la derivada del SHA-256 declarado, que el
    objeto existe y que su tamaño coincide (detecta subidas truncadas).
    Deduplica por SHA-256 como el endpoint clásico.
    """
    try:
        video, dup = register_uploaded_video(
            db, get_storage(), key=body.key, sha256=body.sha256,
            filename=body.filename, title=body.title,
            size_bytes=body.size_bytes,
            duration_seconds=body.duration_seconds)
    except UploadTooLargeError as e:
        raise HTTPException(413, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    out = _video_dict(video)
    out["duplicate"] = dup
    out["duplicate_of"] = video.id if dup else None
    return out


def _video_dict(v: Video) -> dict:
    category, source = effective_category(v)
    return {"id": v.id, "title": v.title, "platform": v.platform,
            "duration_seconds": v.duration_seconds, "file_path": v.file_path,
            "file_size_bytes": v.file_size_bytes or 0,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "category": v.category or "", "subcategory": v.subcategory or "",
            "classification_confidence": v.classification_confidence or 0.0,
            "classification_source": v.classification_source or "",
            "manual_override": bool(v.manual_override),
            "effective_category": category, "effective_category_source": source}


@app.get("/api/videos")
def list_videos(db: Session = Depends(get_db)):
    """Lista de vídeos subidos/descargados, más recientes primero, con
    el estado de su último trabajo de proceso y el nº de clips.

    No existía (daba 404): el panel no podía mostrar los vídeos
    subidos ni su estado.
    """
    out = []
    for v in db.query(Video).order_by(Video.id.desc()).all():
        d = _video_dict(v)
        latest = (db.query(Job)
                  .filter(Job.video_id == v.id, Job.kind == "process")
                  .order_by(Job.id.desc()).first())
        d["process_status"] = latest.status if latest else "sin_procesar"
        d["latest_process_job_id"] = latest.id if latest else None
        d["clips_count"] = len(v.clips)
        out.append(d)
    return out


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


def _run_process_in_background(job_id: int):
    """Ejecuta process_job en un hilo con su propia sesión de BD.

    El endpoint /process responde 202 al instante y el trabajo sigue en
    segundo plano: en Render el proxy corta las peticiones largas (502),
    pero el hilo continúa y el panel sondea /api/jobs/{id} hasta ver
    done/error. process_job ya marca el job como error si falla.
    """
    db = SessionLocal()
    try:
        from worker.jobs import process_job
        process_job(db, job_id, get_storage())
    except Exception:
        pass
    finally:
        db.close()


@app.post("/api/videos/{video_id}/process", status_code=202)
def process_video(video_id: int, db: Session = Depends(get_db)):
    """Inicia el procesamiento en segundo plano (202 inmediato).

    Antes era síncrono y Render cortaba la petición con 502 en vídeos
    largos. Ahora el panel sondea GET /api/jobs/{job_id} hasta done/error.
    """
    v = db.query(Video).filter(Video.id == video_id).one_or_none()
    if not v:
        raise HTTPException(404, "video no encontrado")
    job = Job(video_id=v.id, kind="process", status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    threading.Thread(target=_run_process_in_background, args=(job.id,),
                     daemon=True).start()
    return {"job_id": job.id, "status": "queued", "video_id": v.id}


# ---------------------------------------------------------------- descarga por enlace
class FromUrlRequest(BaseModel):
    url: str
    auto_process: bool = True


def _youtube_cookies_local_path() -> str | None:
    """Ruta local del archivo de cookies de YouTube, si está configurado.

    En storage local es el propio archivo; en S3 se descarga a ./data/.
    Devuelve None si no hay cookies subidas. El contenido nunca se
    expone por ningún endpoint.
    """
    storage = get_storage()
    key = settings.youtube_cookies_key
    if not storage.exists(key):
        return None
    if isinstance(storage, LocalStorage):
        return str(Path(storage.root) / key)
    tmp = "./data/cookies_youtube.txt"
    Path(tmp).write_bytes(storage.read(key))
    return tmp


def _run_download_in_background(job_id: int, url: str, auto_process: bool):
    """Descarga el vídeo (yt-dlp) y, si auto_process, encadena el proceso.

    Corre en un hilo: la descarga puede tardar minutos y el endpoint
    responde 202 al instante. El panel sondea el job de descarga y luego
    el job de proceso (mismo video_id) en /api/jobs.
    """
    db = SessionLocal()
    tmp_dir = Path(f"./data/tmp_download_{job_id}")
    try:
        job = db.query(Job).filter(Job.id == job_id).one()
        job.status = "processing"
        db.commit()
        cookies_path = _youtube_cookies_local_path()
        result = download_video_url(
            url, tmp_dir, cookies_path=cookies_path,
            max_duration_s=settings.max_download_duration_seconds,
            max_size_bytes=int(settings.max_upload_gb * 1024 ** 3))
        data = Path(result["path"]).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        dup = db.query(Video).filter(Video.sha256 == sha).first()
        storage = get_storage()
        if dup:
            video = dup
        else:
            key = f"originals/{sha[:12]}.mp4"
            storage.save(key, result["path"])
            video = Video(title=result["title"] or url, source_url=url,
                          platform="youtube",
                          duration_seconds=result["duration_seconds"],
                          file_path=key, file_size_bytes=result["size_bytes"],
                          sha256=sha)
            db.add(video)
            db.commit()
            db.refresh(video)
        job.video_id = video.id
        job.status = "done"
        db.commit()
        if auto_process:
            pjob = Job(video_id=video.id, kind="process", status="queued")
            db.add(pjob)
            db.commit()
            db.refresh(pjob)
            _run_process_in_background(pjob.id)
    except DownloadError as e:
        job = db.query(Job).filter(Job.id == job_id).one_or_none()
        if job:
            job.status = "error"
            job.error = e.message
            db.commit()
    except Exception as e:  # noqa: BLE001
        job = db.query(Job).filter(Job.id == job_id).one_or_none()
        if job:
            job.status = "error"
            job.error = str(e)[:2000]
            db.commit()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        db.close()


@app.post("/api/videos/from-url", status_code=202)
def video_from_url(body: FromUrlRequest, db: Session = Depends(get_db)):
    """Descarga un vídeo propio de YouTube por enlace y lo procesa.

    Solo vídeos de YouTube (watch/youtu.be/shorts). Sin cookies, YouTube
    bloquea la descarga desde servidores en la nube: el job termina en
    error con el mensaje que explica cómo subir las cookies.
    """
    ok, _vid = is_youtube_video_url(body.url)
    if not ok:
        raise HTTPException(
            400, "Solo se admiten enlaces de vídeos de YouTube "
                 "(watch?v=..., youtu.be/..., shorts/...).")
    dup = db.query(Video).filter(Video.source_url == body.url).first()
    if dup:
        return {"video_id": dup.id, "duplicate_of": dup.id,
                "title": dup.title, "status": "duplicate"}
    job = Job(video_id=None, kind="download", status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    threading.Thread(target=_run_download_in_background,
                     args=(job.id, body.url, body.auto_process),
                     daemon=True).start()
    return {"job_id": job.id, "status": "queued", "kind": "download"}


# ---------------------------------------------------------------- cookies de YouTube
@app.post("/api/cookies/youtube")
async def upload_youtube_cookies(file: UploadFile = File(...)):
    """Guarda el cookies.txt de YouTube del usuario (sesión propia).

    El archivo se guarda en el almacenamiento privado y SOLO lo usa
    yt-dlp para descargar los vídeos del usuario. Nunca se sirve ni se
    muestra su contenido por ningún endpoint.
    """
    raw = await file.read()
    if len(raw) > 1024 * 1024:
        raise HTTPException(413, "El archivo de cookies no puede superar 1 MB")
    text = raw.decode("utf-8", errors="ignore")
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    yt_lines = [ln for ln in lines if "youtube.com" in ln]
    if not yt_lines:
        raise HTTPException(
            400, "El archivo no contiene cookies de youtube.com. "
                 "Exporta las cookies estando en youtube.com con tu sesión iniciada.")
    storage = get_storage()
    Path("./data").mkdir(parents=True, exist_ok=True)
    tmp = "./data/cookies_youtube_upload.txt"
    Path(tmp).write_bytes(raw)
    storage.save(settings.youtube_cookies_key, tmp)
    meta = {"uploaded_at": datetime.now(timezone.utc).isoformat(),
            "youtube_cookies": len(yt_lines), "total_lines": len(lines)}
    meta_tmp = "./data/cookies_youtube_meta.json"
    Path(meta_tmp).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    storage.save(settings.youtube_cookies_meta_key, meta_tmp)
    os.remove(tmp)
    os.remove(meta_tmp)
    return {"uploaded": True, **meta}


@app.get("/api/cookies/youtube/status")
def youtube_cookies_status():
    """Solo metadatos (existe, fecha, nº de cookies). Nunca el contenido."""
    storage = get_storage()
    if not storage.exists(settings.youtube_cookies_key):
        return {"configured": False}
    meta: dict = {"configured": True}
    if storage.exists(settings.youtube_cookies_meta_key):
        try:
            meta.update(json.loads(storage.read(settings.youtube_cookies_meta_key).decode("utf-8")))
        except Exception:
            pass
    return meta


@app.delete("/api/cookies/youtube")
def delete_youtube_cookies():
    storage = get_storage()
    storage.delete(settings.youtube_cookies_key)
    storage.delete(settings.youtube_cookies_meta_key)
    return {"deleted": True}


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
