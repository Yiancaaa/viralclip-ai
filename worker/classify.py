"""Clasificación del tipo de directo (VIRALCLIP AI, v0.2).

Clasifica un vídeo en una de las categorías principales a partir de
MUESTRAS reales de audio y vídeo (nunca del vídeo entero: RAM ~1 GB libre):

- vídeo: fotogramas a 1 fps (máx. 60), rostros estables con OpenCV Haar,
  movimiento / cambio entre fotogramas y cortes de escena.
- audio: transcripción faster-whisper (modelo tiny, solo la muestra) ->
  proporción de habla vs silencio, velocidad de habla, duración de los
  turnos de palabra, preguntas, interrupciones aparentes y vocabulario.

Categorías principales y subcategorías internas::

    gameplay_streamer   -> gameplay_solo | gameplay_facecam
    podcast_entrevista  -> podcast_estudio
    reality_casa        -> reality_multi_cam
    conversacion_reaccion -> conversacion_solo | narracion_solo
    otros               -> otros

Un reality con varias cámaras y participantes entrando/saliendo NO es un
podcast aunque ambos sean conversación: el podcast es plano fijo con
rostros estables y turnos largos; el reality tiene cortes de cámara,
número de rostros variable y turnos cortos con interrupciones.

Si ``confidence < 0.6`` el resultado lleva ``needs_user_choice=True``: el
sistema debe preguntar al usuario (los dos botones del dashboard) en vez
de decidir solo. La selección manual tiene prioridad (ver
``effective_category``) y queda registrada como ``manual_override=True``.

Presupuesto 0 $: solo open source (OpenCV Haar + faster-whisper).
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

CATEGORIES = ["gameplay_streamer", "podcast_entrevista", "reality_casa",
              "conversacion_reaccion", "otros"]

SUBCATEGORIES = {
    "gameplay_streamer": ["gameplay_solo", "gameplay_facecam"],
    "podcast_entrevista": ["podcast_estudio"],
    "reality_casa": ["reality_multi_cam"],
    "conversacion_reaccion": ["conversacion_solo", "narracion_solo"],
    "otros": ["otros"],
}

# Mapeo subcategoría -> categoría principal (validación de la corrección manual).
SUBCATEGORY_TO_CATEGORY = {sub: cat for cat, subs in SUBCATEGORIES.items() for sub in subs}

# Botones del dashboard (frontend): qué categoría cae bajo cada botón.
#   Botón 1 "Gameplay/Streamer"  -> gameplay_streamer
#   Botón 2 "Podcast/Reality/Conversación" -> podcast_entrevista,
#   reality_casa, conversacion_reaccion. "otros" no tiene botón: el usuario
#   elige una de las cuatro o deja la detección automática.
BUTTONS = {
    "gameplay_streamer": "Gameplay/Streamer",
    "podcast_entrevista": "Podcast/Reality/Conversación",
    "reality_casa": "Podcast/Reality/Conversación",
    "conversacion_reaccion": "Podcast/Reality/Conversación",
    "otros": None,
}

CONFIDENCE_THRESHOLD = 0.6  # por debajo, preguntar al usuario

# --- vocabulario de señales (texto normalizado, sin tildes) -----------------
GAME_WORDS = {"juego", "jugando", "partida", "nivel", "mision", "gana", "gane",
              "gano", "victoria", "derrota", "enemigo", "jefe", "boss", "mapa",
              "arma", "disparo", "corre", "salta", "mira", "vamos", "vamos a",
              "nooo", "vamooos", "dale", "ojo", "cuidado", "trampa", "roblox",
              "minecraft", "fortnite", "gta", "fifa", "jugador", "equipo",
              "ronda", "puntos", "kill", "muerte", "revive", "campeon"}
CONVO_WORDS = {"pregunta", "opinas", "crees", "piensas", "por que", "cuando",
               "donde", "quien", "entrevista", "invitado", "hablemos", "debate",
               "discusion", "tema", "historia", "cuento", "recuerdo", "ayer",
               "entonces", "despues", "resulta", "nadie sabe", "misterio"}
NARRATION_WORDS = {"erase", "habia una vez", "aquella noche", "cada noche",
                   "de repente", "entonces", "al final", "nadie", "nunca",
                   "siempre", "casa", "noche", "durmiendo", "desperto",
                   "silencio", "oscuridad", "ventana", "puerta", "foto"}
INTERRUPTION_MARKERS = ("pero", "no, no", "espera", "dejame", "oye", "—")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ------------------------------------------------------------------ vídeo
def extract_video_signals(video_path: str, max_seconds: int = 60,
                          fps: int = 1, width: int = 320) -> dict:
    """Muestrea fotogramas a 1 fps (máx. `max_seconds`) y mide rostros,
    movimiento y cortes. No carga el vídeo entero en memoria.

    Devuelve señales reales; si OpenCV no está instalado, las señales de
    rostro son 0 y ``opencv_available`` es False (la clasificación sigue
    con audio + movimiento, con menos confianza).
    """
    signals: dict = {"opencv_available": False, "frames_sampled": 0,
                     "mean_faces": 0.0, "max_faces": 0, "face_frames_ratio": 0.0,
                     "face_count_std": 0.0, "facecam_ratio": 0.0,
                     "motion_mean": 0.0, "motion_high_ratio": 0.0,
                     "cut_ratio": 0.0, "mean_face_x": None}
    try:
        import cv2
    except ImportError:
        return signals
    signals["opencv_available"] = True
    cascade_path = os.path.join(cv2.data.haarcascades,
                                "haarcascade_frontalface_default.xml")
    cascade = cv2.CascadeClassifier(cascade_path)
    if cascade.empty():
        return signals

    with tempfile.TemporaryDirectory(prefix="viralclip_frames_") as tmp:
        pattern = os.path.join(tmp, "f%03d.jpg")
        cmd = ["ffmpeg", "-y", "-v", "error", "-t", str(max_seconds),
               "-i", video_path, "-vf", f"fps={fps},scale={width}:-1", pattern]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return signals
        files = sorted(Path(tmp).glob("f*.jpg"))
        if not files:
            return signals
        signals["frames_sampled"] = len(files)
        prev_gray = None
        face_counts: list[int] = []
        face_xs: list[float] = []
        corner_face_frames = 0
        face_frames = 0
        diffs: list[float] = []
        for f in files:
            img = cv2.imread(str(f))
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1,
                                             minNeighbors=5, minSize=(30, 30))
            face_counts.append(len(faces))
            if len(faces):
                face_frames += 1
                for (x, y, fw, fh) in faces:
                    cx = (x + fw / 2) / w
                    cy = (y + fh / 2) / h
                    face_xs.append(cx)
                    # Facecam típica: rostro pequeño en una esquina superior.
                    if (cx < 0.35 or cx > 0.65) and cy < 0.45 and fw < 0.45 * w:
                        corner_face_frames += 1
                        break
            if prev_gray is not None and prev_gray.shape == gray.shape:
                diffs.append(float((cv2.absdiff(prev_gray, gray).mean()) / 255.0))
            prev_gray = gray
        if not face_counts:
            return signals
        mean_faces = sum(face_counts) / len(face_counts)
        mean = mean_faces
        var = sum((c - mean) ** 2 for c in face_counts) / len(face_counts)
        signals.update({
            "mean_faces": round(mean_faces, 2),
            "max_faces": max(face_counts),
            "face_frames_ratio": round(face_frames / len(face_counts), 2),
            "face_count_std": round(math.sqrt(var), 2),
            "facecam_ratio": round(corner_face_frames / max(face_frames, 1), 2),
            "mean_face_x": round(sum(face_xs) / len(face_xs), 2) if face_xs else None,
        })
        if diffs:
            signals.update({
                "motion_mean": round(sum(diffs) / len(diffs), 4),
                "motion_high_ratio": round(sum(1 for d in diffs if d > 0.08) / len(diffs), 2),
                "cut_ratio": round(sum(1 for d in diffs if d > 0.25) / len(diffs), 2),
            })
    return signals


# ------------------------------------------------------------------ audio
def extract_speech_signals(segments, total_duration: float) -> dict:
    """Señales de habla a partir de la transcripción (faster-whisper).

    `segments` es la lista de TranscriptSegment de worker.pipeline (con
    palabras y timestamps). `total_duration` es la duración de la muestra
    en segundos. Sin transcripción, todo es 0 (silencio/música).
    """
    words: list = []
    for s in segments or []:
        words.extend(getattr(s, "words", []) or [])
    text = " ".join(getattr(s, "text", "") for s in (segments or []))
    norm = _norm(text)
    speech_seconds = sum(max(0.0, float(getattr(s, "end", 0)) - float(getattr(s, "start", 0)))
                         for s in (segments or []))
    total = max(float(total_duration or 0.0), 1.0)
    speech_ratio = _clip(speech_seconds / total)
    n_words = len(words)
    wpm = (n_words / max(speech_seconds, 1.0)) * 60.0 if speech_seconds > 0 else 0.0
    durations = [float(getattr(s, "end", 0)) - float(getattr(s, "start", 0))
                 for s in (segments or [])]
    mean_turn = sum(durations) / len(durations) if durations else 0.0
    # Interrupciones aparentes: turnos muy cortos (<2,5 s) y segmentos que
    # terminan sin puntuación final (frase cortada). Es una aproximación:
    # sin diarización real no se puede saber con certeza quién habla.
    short_turns = sum(1 for d in durations if d < 2.5)
    cut_off = sum(1 for s in (segments or [])
                  if getattr(s, "text", "").strip()
                  and not getattr(s, "text", "").strip().endswith((".", "!", "?", "…")))
    n_seg = max(len(segments or []), 1)
    questions = text.count("?") + norm.count("¿")
    game_hits = sum(1 for w in GAME_WORDS if w in norm)
    convo_hits = sum(1 for w in CONVO_WORDS if w in norm)
    narration_hits = sum(1 for w in NARRATION_WORDS if w in norm)
    return {
        "speech_ratio": round(speech_ratio, 2),
        "words_total": n_words,
        "wpm": round(wpm, 1),
        "mean_turn_seconds": round(mean_turn, 1),
        "turns": len(segments or []),
        "short_turn_ratio": round(short_turns / n_seg, 2),
        "cut_off_ratio": round(cut_off / n_seg, 2),
        "questions": questions,
        "game_word_hits": game_hits,
        "convo_word_hits": convo_hits,
        "narration_word_hits": narration_hits,
        "transcript_chars": len(text),
    }


# ------------------------------------------------------- decisión por reglas
def classify_from_signals(video_signals: dict, speech_signals: dict) -> dict:
    """Decisión determinista a partir de las señales ya extraídas.

    Puntuación por categoría (0..1) con pesos documentados; gana la mayor.
    La confianza sale del margen entre la primera y la segunda: si las
    señales no separan claramente dos categorías, la confianza baja y se
    pide elegir al usuario.
    """
    v = video_signals or {}
    s = speech_signals or {}
    motion = _clip(v.get("motion_mean", 0.0) / 0.12)  # ~0.12 = movimiento alto
    motion_high = v.get("motion_high_ratio", 0.0)
    cuts = v.get("cut_ratio", 0.0)
    mean_faces = v.get("mean_faces", 0.0)
    max_faces = v.get("max_faces", 0)
    face_ratio = v.get("face_frames_ratio", 0.0)
    face_std = v.get("face_count_std", 0.0)
    facecam = v.get("facecam_ratio", 0.0)
    speech = s.get("speech_ratio", 0.0)
    wpm = s.get("wpm", 0.0)
    mean_turn = s.get("mean_turn_seconds", 0.0)
    short_turns = s.get("short_turn_ratio", 0.0)
    cut_off = s.get("cut_off_ratio", 0.0)
    questions = s.get("questions", 0)
    game_hits = s.get("game_word_hits", 0)
    convo_hits = s.get("convo_word_hits", 0)
    narration_hits = s.get("narration_word_hits", 0)
    has_speech = speech > 0.15 and s.get("words_total", 0) >= 10

    # Rostros: estables = presentes en la mayoría de fotogramas con poca
    # variación en el número; variables = entran/salen (reality).
    faces_stable = face_ratio > 0.5 and face_std < 0.8
    faces_variable = face_ratio > 0.3 and face_std >= 0.8
    multi_face = max_faces >= 2 and face_ratio > 0.3

    scores: dict[str, float] = {}
    # Gameplay: mucho movimiento/cambio de escena, poco o nada de habla
    # sostenida, vocabulario de juego; la facecam (si existe) es un rostro
    # pequeño en esquina, no el plano principal.
    scores["gameplay_streamer"] = _clip(
        0.35 * motion + 0.15 * motion_high
        + 0.20 * (1.0 - speech if has_speech else 1.0)
        + 0.15 * (1.0 if game_hits >= 2 else (0.5 if game_hits == 1 else 0.0))
        + 0.15 * (1.0 if (mean_faces < 0.5 or facecam > 0.3) else 0.0))
    # Podcast: plano fijo (poco movimiento), 2+ rostros estables, mucha
    # habla con turnos largos, vocabulario de conversación/entrevista.
    scores["podcast_entrevista"] = _clip(
        0.25 * (1.0 - motion)
        + 0.30 * (1.0 if (multi_face and faces_stable) else (0.4 if multi_face else 0.0))
        + 0.20 * speech
        + 0.15 * (1.0 if mean_turn >= 8 else (0.5 if mean_turn >= 4 else 0.0))
        + 0.10 * (1.0 if convo_hits >= 2 else (0.5 if convo_hits == 1 else 0.0)))
    # Reality: varias personas, número de rostros variable, cortes de
    # cámara, mucha habla con turnos cortos e interrupciones aparentes.
    scores["reality_casa"] = _clip(
        0.20 * _clip(cuts * 3.0) + 0.10 * motion
        + 0.25 * (1.0 if (multi_face and faces_variable) else (0.4 if multi_face else 0.0))
        + 0.15 * speech
        + 0.15 * (1.0 if short_turns > 0.4 else (0.5 if short_turns > 0.2 else 0.0))
        + 0.15 * _clip(cut_off))
    # Conversación/reacción (incluye narración de una sola voz): mucha
    # habla, 0-1 rostros, poco movimiento, turnos largos y fluidos.
    scores["conversacion_reaccion"] = _clip(
        0.30 * speech
        + 0.20 * (1.0 - motion)
        + 0.15 * (1.0 if mean_faces <= 1.0 else 0.0)
        + 0.15 * (1.0 if mean_turn >= 6 else (0.5 if mean_turn >= 3 else 0.0))
        + 0.10 * (1.0 if questions >= 1 else 0.0)
        + 0.10 * (1.0 if convo_hits >= 1 or narration_hits >= 2 else 0.0))
    # Otros: red de seguridad cuando ninguna señal es fuerte.
    scores["otros"] = 0.30 if max(scores.values()) < 0.45 else 0.15

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_cat, top_score = ranked[0]
    second_score = ranked[1][1]
    margin = top_score - second_score
    # Confianza por margen; si la mejor puntuación es baja en absoluto,
    # la confianza también baja aunque haya margen.
    confidence = _clip(0.35 + 0.65 * min(1.0, margin / 0.4))
    if top_score < 0.45:
        confidence *= 0.7
    if not v.get("opencv_available", True):
        confidence *= 0.85  # sin rostros, la decisión es menos fiable
    # Nunca 1.0: la clasificación por muestras es una estimación, no una certeza.
    confidence = round(_clip(confidence, 0.0, 0.95), 2)

    subcategory = _subcategory(top_cat, v, s, narration_hits)
    signals = {"video": v, "speech": s, "scores": {k: round(x, 2) for k, x in scores.items()}}
    return {
        "category": top_cat,
        "subcategory": subcategory,
        "confidence": confidence,
        "needs_user_choice": confidence < CONFIDENCE_THRESHOLD,
        "signals": signals,
        "layout": layout_for_category(top_cat, v),
    }


def _subcategory(category: str, v: dict, s: dict, narration_hits: int) -> str:
    if category == "gameplay_streamer":
        # Facecam: rostro estable en esquina en >30% de los fotogramas con rostro.
        if v.get("facecam_ratio", 0.0) > 0.3 and v.get("mean_faces", 0.0) >= 0.5:
            return "gameplay_facecam"
        return "gameplay_solo"
    if category == "podcast_entrevista":
        return "podcast_estudio"
    if category == "reality_casa":
        return "reality_multi_cam"
    if category == "conversacion_reaccion":
        # Narración de una sola voz: turnos medios/largos, sin rostros (o
        # uno ilustrado), vocabulario narrativo y pocas preguntas directas.
        # El umbral de turno es 4 s (no 6): faster-whisper tiny corta la
        # narración en segmentos de ~4-5 s aunque sea una sola voz.
        if (s.get("mean_turn_seconds", 0.0) >= 4.0 and s.get("questions", 0) <= 1
                and (narration_hits >= 2 or v.get("mean_faces", 0.0) < 0.5)):
            return "narracion_solo"
        return "conversacion_solo"
    return "otros"


def layout_for_category(category: str, video_signals: dict | None = None) -> dict:
    """Cómo debe renderizar el clip según la categoría.

    - gameplay: el juego llena el frame; si hay facecam, se reserva (PiP).
    - conversación/podcast/reality: foco en el rostro / la persona que habla
      (el render usa `mean_face_x` para desplazar el recorte).
    - otros: recorte centrado neutro.
    """
    v = video_signals or {}
    if category == "gameplay_streamer":
        facecam = bool(v.get("facecam_ratio", 0.0) > 0.3)
        return {"layout": "gameplay_facecam" if facecam else "gameplay_full",
                "focus": "gameplay", "facecam_pip": facecam,
                "focus_x": None}
    if category in ("podcast_entrevista", "reality_casa", "conversacion_reaccion"):
        return {"layout": "face_focus", "focus": "face",
                "facecam_pip": False,
                "focus_x": v.get("mean_face_x")}
    return {"layout": "center", "focus": "center", "facecam_pip": False,
            "focus_x": None}


def effective_category(video) -> tuple[str, str]:
    """Categoría efectiva para puntuar y renderizar.

    Prioridad: corrección manual del usuario > clasificación automática >
    'otros' (criterios neutros). Devuelve (categoría, origen) donde origen
    es 'manual' | 'auto' | 'default'.
    """
    cat = getattr(video, "category", None) or ""
    if getattr(video, "manual_override", False) and cat in CATEGORIES:
        return cat, "manual"
    if cat in CATEGORIES:
        return cat, "auto"
    return "otros", "default"


# ------------------------------------------------------- clasificación completa
def classify_video(video_path: str, sample_seconds: int = 60,
                   whisper_model: str = "tiny") -> dict:
    """Clasifica un vídeo real: muestrea vídeo (1 fps) y audio (primeros
    `sample_seconds`) y aplica `classify_from_signals`. Usa el modelo
    whisper `tiny` para que la transcripción de la muestra sea rápida y
    quepa en ~1 GB de RAM.
    """
    from worker.pipeline import extract_audio, probe_duration, transcribe_subprocess

    duration = probe_duration(video_path)
    sample = min(float(sample_seconds), duration)
    video_signals = extract_video_signals(video_path, max_seconds=int(sample))
    speech_signals: dict = {"speech_ratio": 0.0, "words_total": 0,
                            "wpm": 0.0, "mean_turn_seconds": 0.0, "turns": 0,
                            "short_turn_ratio": 0.0, "cut_off_ratio": 0.0,
                            "questions": 0, "game_word_hits": 0,
                            "convo_word_hits": 0, "narration_word_hits": 0,
                            "transcript_chars": 0}
    with tempfile.TemporaryDirectory(prefix="viralclip_classify_") as tmp:
        audio = os.path.join(tmp, "sample.wav")
        # Extraer solo la muestra de audio (no el audio entero).
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-t", str(sample),
                        "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", audio],
                       check=True, capture_output=True, text=True)
        try:
            # En subproceso aparte: el modelo nunca se carga en el
            # proceso web (512 MB en el plan Free de Render).
            segments = transcribe_subprocess(audio, model=whisper_model)
        except Exception:
            segments = []
        speech_signals = extract_speech_signals(segments, sample)
    result = classify_from_signals(video_signals, speech_signals)
    result["sample_seconds"] = sample
    result["duration_seconds"] = duration
    return result


def classification_to_record(result: dict) -> dict:
    """Campos para guardar en el modelo Video."""
    return {
        "category": result["category"],
        "subcategory": result["subcategory"],
        "classification_confidence": result["confidence"],
        "classification_source": "auto",
        "manual_override": False,
        "classification_signals": json.dumps(result["signals"], ensure_ascii=False),
    }
