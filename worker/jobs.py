"""Ejecución de un trabajo de procesamiento: transcribir -> detectar ->
puntuar -> titular -> renderizar -> guardar en biblioteca. Real, síncrono."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from sqlalchemy.orm import Session

from viralclip.config import settings
from viralclip.models import Clip, Job, Moment, Video
from viralclip.storage import Storage
from worker import pipeline
from worker.classify import effective_category

# Detección de facecam en process_job: muestreo acotado (no el vídeo
# entero) para no alargar el proceso.
VISION_SAMPLE_SECONDS = 30.0
VISION_SAMPLE_FPS = 1.0
VISION_SAMPLE_LIMIT = 30

# BLOQUEO DE PROCESO (job lock): en Render Free solo caben 512 MB y un
# solo proceso de vídeo a la vez; dos renders simultáneos tumbarían el
# servidor por falta de memoria (OOM 2026-11-26). El bloqueo vive en BD
# (estado del Job), así que sobrevive a reinicios del proceso web: un job
# de proceso ocupa el slot mientras está "queued" o "processing". El slot
# lo ocupa el job activo MÁS ANTIGUO (menor id): si dos peticiones llegan
# a la vez, gana el primero y el otro queda en cola; así no hay empate en
# el que ambos se retiren y nadie procese.
PROCESS_ACTIVE_STATUSES = ("queued", "processing")
# Candado en proceso para la comprobación+reclamación: uvicorn corre con
# un solo worker, así que todos los hilos de proceso son del mismo
# proceso y este candado cierra la carrera entre dos hilos.
_CLAIM_LOCK = threading.Lock()


def detect_render_layout(local_src: str, category: str) -> tuple[dict | None, bool]:
    """Detecta la facecam del vídeo para decidir el layout del render.

    Solo se intenta cuando la categoría efectiva es `gameplay_streamer`
    (la clasificación por categoría — `layout_for_category` en
    worker/classify.py — solo dice si *podría* haber facecam PiP, a partir
    de señales; la detección real con OpenCV es la que decide). Muestrea
    como máximo 30 fotogramas a 1 fps de los primeros 30 s.

    Devuelve (facecam_dict_o_None, vision_disponible). Sin OpenCV/cv2 o
    si la detección falla por cualquier motivo, devuelve (None, False)
    y el job sigue con layout `gameplay_only`: la detección es una
    mejora del render, nunca un motivo para romper el job.
    """
    if category != "gameplay_streamer":
        return None, False
    try:
        from worker import vision
        frames = vision.sample_frames(local_src, fps=VISION_SAMPLE_FPS,
                                      max_seconds=VISION_SAMPLE_SECONDS,
                                      limit=VISION_SAMPLE_LIMIT)
        if not frames:
            return None, False
        return vision.detect_facecam(frames), True
    except Exception:
        return None, False


def layout_for_moment(facecam: dict | None, vision_available: bool,
                      scored: dict) -> str:
    """Layout de un momento a partir de la facecam detectada (una vez por
    vídeo) y de la emoción del momento: un momento de reacción (emoción
    alta) sube la facecam arriba (`facecam_top`); si no, gameplay arriba
    (`facecam_bottom`). Sin visión disponible, `gameplay_only`."""
    if not vision_available:
        return "gameplay_only"
    try:
        from worker.vision import decide_layout
        emotion = scored.get("breakdown", {}).get("emocion", {}).get("score", 0.0) / 100.0
        return decide_layout(facecam or {}, None, {"emotion_score": emotion})
    except Exception:
        return "gameplay_only"


def active_process_job(db: Session, exclude_job_id: int | None = None) -> Job | None:
    """El job de proceso que ocupa el único slot de render (o None).

    Es el job activo MÁS ANTIGUO (menor id): un job en "queued" también
    ocupa el slot, porque su hilo está a punto de arrancar. El endpoint
    debe llamar a esta función ANTES de crear un job nuevo: si devuelve
    un job, responde 409 ("ya hay un proceso en marcha") en vez de
    crear otro; process_job es la segunda línea de defensa.
    """
    q = (db.query(Job).filter(Job.kind == "process",
                              Job.status.in_(PROCESS_ACTIVE_STATUSES))
         .order_by(Job.id.asc()))
    if exclude_job_id is not None:
        q = q.filter(Job.id != exclude_job_id)
    return q.first()


def try_claim_process_slot(db: Session, job_id: int) -> bool:
    """Reclama el slot de render para este job (lo pone en "processing").

    Devuelve True solo si este job es el activo más antiguo (o el único):
    cualquier otro job activo con menor id gana el slot y este devuelve
    False sin tocar su estado. La regla del menor id es determinista, así
    que dos hilos que reclamen a la vez llegan al mismo resultado; el
    candado en proceso cierra además la carrera entre la comprobación y
    el cambio de estado.
    """
    with _CLAIM_LOCK:
        job = db.query(Job).filter(Job.id == job_id).one()
        if job.status not in PROCESS_ACTIVE_STATUSES:
            return False
        holder = active_process_job(db)
        if holder is not None and holder.id != job.id:
            return False
        job.status = "processing"
        db.commit()
        return True


def queue_position(db: Session, job_id: int) -> int:
    """Posición 1-based de un job en la cola de proceso (1 = procesando
    ahora). Solo tiene sentido para jobs en "queued"/"processing"."""
    ahead = (db.query(Job).filter(Job.kind == "process",
                                  Job.status.in_(PROCESS_ACTIVE_STATUSES),
                                  Job.id < job_id).count())
    return ahead + 1


def _run_single_process_job(db: Session, job_id: int, storage: Storage, workdir: str = "./data/work") -> dict:
    """Un solo proceso completo (el slot ya está reclamado: el job está
    en "processing"). Transcribir -> detectar -> puntuar -> titular ->
    renderizar -> guardar en biblioteca."""
    job = db.query(Job).filter(Job.id == job_id).one()
    # Temporales de este trabajo para limpiarlos al final: en Render el
    # disco del contenedor es limitado y los originales/audios/clips se
    # acumulan job tras job (los clips ya están subidos a storage).
    # El origen NO se borra si era una ruta local preexistente.
    temp_files: list[str] = []
    try:
        video = db.query(Video).filter(Video.id == job.video_id).one()
        src = video.file_path
        if not storage.exists(src):
            # file_path puede ser ruta local absoluta (subida directa)
            if not os.path.exists(src):
                raise FileNotFoundError(f"origen no encontrado: {src}")
            local_src = src
        else:
            local_src = os.path.join(workdir, f"src_{video.id}.mp4")
            Path(local_src).parent.mkdir(parents=True, exist_ok=True)
            Path(local_src).write_bytes(storage.read(src))
            temp_files.append(local_src)
        # La duración del vídeo subido llega a 0.0 (el endpoint no la
        # mide); medirla aquí una vez descargado el origen, sin romper
        # el job si la medición falla.
        if not video.duration_seconds:
            try:
                video.duration_seconds = pipeline.probe_duration(local_src)
                db.commit()
            except Exception:
                pass
        audio = os.path.join(workdir, f"audio_{video.id}.wav")
        temp_files.append(audio)
        pipeline.extract_audio(local_src, audio)
        # Modelo CONFIGURADO (settings.whisper_model), no el default de
        # transcribe(): en vivo es 'tiny' por la RAM (512 MB en Render
        # Free); llamar sin modelo usaba 'small' (~244 MB) y tumbaba el
        # servidor por falta de memoria (OOM 2026-11-26). La transcripción
        # corre en un subproceso aparte (transcribe_subprocess): el modelo
        # y sus librerías nunca se cargan en el proceso web.
        segments = pipeline.transcribe_subprocess(
            audio, model=settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type)
        # Categoría efectiva: manual > automática > 'otros' (neutro).
        category, cat_source = effective_category(video)
        focus_x = None
        try:
            import json as _json
            sig = _json.loads(video.classification_signals or "{}")
            focus_x = sig.get("video", {}).get("mean_face_x")
        except Exception:
            focus_x = None
        moments = pipeline.detect_moments(segments)
        # Palabras con timestamp (transcripción completa) para los
        # subtítulos cinéticos; layout con facecam solo en gameplay.
        words = [w for s in segments for w in s.words]
        facecam_info, vision_available = detect_render_layout(local_src, category)
        made = []
        for mo in moments:
            scored = pipeline.score_moment(mo, category=category)
            titles = pipeline.generate_titles(mo["text"])
            m = Moment(job_id=job.id, video_id=video.id,
                       start_seconds=mo["start"], end_seconds=mo["end"],
                       score=scored["score"], score_breakdown=json.dumps(scored["breakdown"], ensure_ascii=False),
                       transcript_text=mo["text"], titles=json.dumps(titles, ensure_ascii=False))
            db.add(m)
            db.flush()
            subs = pipeline.subtitles_from_segments(segments, mo["start"], mo["end"])
            clip_local = os.path.join(workdir, f"clip_{m.id}.mp4")
            temp_files.append(clip_local)
            # Layout por momento (reacción -> facecam_top, si no
            # facecam_bottom) y subtítulos dinámicos por defecto cuando
            # hay palabras; sin palabras, estático. Sin facecam o sin
            # gameplay, gameplay_only con recorte centrado/desplazado
            # (focus_x en conversación) y subtítulos dinámicos.
            layout = layout_for_moment(facecam_info, vision_available, scored)
            style = "dynamic" if words else "static"
            subtitle_position = "raised" if layout == "facecam_bottom" else "bottom"
            pipeline.render_clip(local_src, mo["start"], mo["end"], subs, clip_local,
                                 category=category, focus_x=focus_x,
                                 layout=layout, facecam=facecam_info,
                                 style=style,
                                 words=words if style == "dynamic" else None,
                                 subtitle_position=subtitle_position)
            thumb_local = os.path.join(workdir, f"thumb_{m.id}.jpg")
            temp_files.append(thumb_local)
            pipeline.make_thumbnail(local_src, mo["start"] + 1.0, thumb_local)
            key = f"clips/{m.id}.mp4"
            thumb_key = f"thumbs/{m.id}.jpg"
            storage.save(key, clip_local)
            storage.save(thumb_key, thumb_local)
            try:
                clip_duration = pipeline.probe_duration(clip_local)
            except Exception:
                # El recorte dinámico de silencios acorta el clip; si no
                # se puede medir, se guarda la duración del momento.
                clip_duration = mo["end"] - mo["start"]
            clip = Clip(video_id=video.id, moment_id=m.id, title=titles[0] if titles else "",
                        platform=video.platform, duration_seconds=clip_duration,
                        score=scored["score"], file_path=key, thumbnail_path=thumb_key,
                        file_size_bytes=os.path.getsize(clip_local))
            db.add(clip)
            made.append({"moment_id": m.id, "score": scored["score"], "clip": key,
                         "layout": layout, "style": style})
        job.status = "done"
        db.commit()
        return {"job_id": job.id, "status": "done", "moments": len(made), "clips": made,
                "category": category, "category_source": cat_source,
                "facecam_present": bool(facecam_info and facecam_info.get("present")),
                "vision_available": vision_available}
    except Exception as e:
        job.status = "error"
        job.error = str(e)[:2000]
        db.commit()
        raise
    finally:
        for path in temp_files:
            try:
                os.remove(path)
            except OSError:
                pass


def process_job(db: Session, job_id: int, storage: Storage, workdir: str = "./data/work") -> dict:
    """Punto de entrada del worker (lo llama _run_process_in_background).

    Reclama el único slot de render; si otro proceso está en marcha, el
    job queda en "queued" (sin marcarse error) y lo drena el job que
    tiene el slot cuando termina. Al terminar (bien o mal) drena la cola:
    los trabajos encolados se procesan de uno en uno, en orden de llegada.
    El 409 para el panel lo debe devolver el endpoint ANTES de crear el
    job (ver active_process_job); process_job es la segunda línea de
    defensa: nunca dos renders a la vez aunque la petición llegue por
    otro camino (p. ej. la descarga por enlace encadena el proceso).
    """
    job = db.query(Job).filter(Job.id == job_id).one()
    if job.status not in PROCESS_ACTIVE_STATUSES:
        return {"job_id": job.id, "status": job.status, "detail": "job_not_active"}
    if not try_claim_process_slot(db, job_id):
        return {"job_id": job.id, "status": "queued", "detail": "process_slot_busy",
                "message": "Ya hay otro vídeo procesándose; este trabajo queda en cola."}
    try:
        return _run_single_process_job(db, job_id, storage, workdir)
    finally:
        _drain_process_queue(db, storage, workdir)


def _drain_process_queue(db: Session, storage: Storage, workdir: str = "./data/work") -> None:
    """Procesa en orden los jobs de proceso encolados, de uno en uno.

    Solo la corre el hilo del job que acaba de terminar (o fallar): un
    job encolado nunca se queda esperando a que alguien lo despierte. Si
    un vídeo falla, queda en error y la cola sigue con el siguiente.
    """
    while True:
        nxt = (db.query(Job).filter(Job.kind == "process", Job.status == "queued")
               .order_by(Job.id.asc()).first())
        if nxt is None:
            return
        if not try_claim_process_slot(db, nxt.id):
            return  # otro hilo tiene el slot; él drenará la cola
        try:
            _run_single_process_job(db, nxt.id, storage, workdir)
        except Exception:
            # _run_single_process_job ya marcó el job como error.
            continue
