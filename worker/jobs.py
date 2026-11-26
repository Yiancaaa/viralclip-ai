"""Ejecución de un trabajo de procesamiento: transcribir -> detectar ->
puntuar -> titular -> renderizar -> guardar en biblioteca. Real, síncrono."""
from __future__ import annotations

import json
import os
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


def process_job(db: Session, job_id: int, storage: Storage, workdir: str = "./data/work") -> dict:
    job = db.query(Job).filter(Job.id == job_id).one()
    job.status = "processing"
    db.commit()
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
        audio = os.path.join(workdir, f"audio_{video.id}.wav")
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
