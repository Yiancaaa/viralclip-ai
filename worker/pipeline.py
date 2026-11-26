"""Pipeline real: captura, transcripción, detección de momentos, puntuación,
títulos y render vertical 9:16 con subtítulos ASS quemados.

Ideas reutilizadas de build_video.py (La casa...): subtítulos ASS con
palabra clave resaltada y montaje por beats con ffmpeg.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from worker.kinetic_subs import zoom_crop_expression

# ---------------------------------------------------------------- utilidades
def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def probe_duration(path: str) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        text=True,
    ).strip()
    return float(out)


# ---------------------------------------------------------------- captura
def capture_segment(url: str, duration: float, out_path: str, quality: str = "best") -> str:
    """Captura `duration` segundos de un directo por HLS con streamlink
    (contenido propio/autorizado). streamlink escribe a stdout -> ffmpeg lo
    corta a `duration` y guarda MP4. Si streamlink no está, error claro."""
    if shutil.which("streamlink") is None:
        raise RuntimeError("streamlink no instalado (pip install streamlink)")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    sl = subprocess.Popen(
        ["streamlink", "--stdout", url, quality],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        ff = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", "pipe:0", "-t", str(duration),
             "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", out_path],
            stdin=sl.stdout, capture_output=True, text=True, timeout=int(duration) + 120,
        )
    finally:
        sl.terminate()
    if ff.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"captura fallida: {ff.stderr[-500:]}")
    return out_path


def extract_audio(video_path: str, out_path: str) -> str:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error", "-i", video_path, "-vn",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out_path])
    return out_path


# ---------------------------------------------------------------- transcripción
@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class TranscriptSegment:
    text: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)


def transcribe(audio_path: str, model: str = "small", device: str = "cpu",
               compute_type: str = "int8", language: str = "es") -> list[TranscriptSegment]:
    """faster-whisper real, con timestamps de palabra (word_timestamps=True).
    Modelo por defecto 'small' en CPU para desarrollo; 'large-v3-turbo'
    documentado como opción con más RAM/GPU."""
    from faster_whisper import WhisperModel

    m = WhisperModel(model, device=device, compute_type=compute_type)
    segments, _info = m.transcribe(audio_path, language=language, word_timestamps=True,
                                   vad_filter=True)
    out: list[TranscriptSegment] = []
    for s in segments:
        words = [Word(w.word.strip(), float(w.start), float(w.end))
                 for w in (s.words or []) if w.word.strip()]
        out.append(TranscriptSegment(s.text.strip(), float(s.start), float(s.end), words))
    return out


def segments_to_json(segments: list[TranscriptSegment]) -> list[dict]:
    return [{"text": s.text, "start": s.start, "end": s.end,
             "words": [{"text": w.text, "start": w.start, "end": w.end} for w in s.words]}
            for s in segments]


# ---------------------------------------------------------------- detección de momentos
def detect_moments(segments: list[TranscriptSegment], audio_energy: list[float] | None = None,
                   min_len: float = 15.0, max_len: float = 90.0,
                   target_len: float = 45.0) -> list[dict]:
    """Ventanas de 30-90 s sobre la transcripción, cortadas SOLO en límites
    de palabra (nunca a mitad de palabra) y preferentemente en pausas
    (>0,8 s entre palabras). `audio_energy` es opcional (lista por segundo,
    0..1) y sube la prioridad de ventanas con picos de energía."""
    words: list[Word] = []
    for s in segments:
        words.extend(s.words)
    if not words:
        # Sin timestamps de palabra: usar límites de segmento.
        return [{"start": s.start, "end": s.end, "text": s.text}
                for s in segments if s.end - s.start >= 5.0]
    # Pausas = fronteras naturales
    boundaries = [0]
    for i in range(1, len(words)):
        if words[i].start - words[i - 1].end >= 0.8:
            boundaries.append(i)
    boundaries.append(len(words))
    # Agrupar palabras en bloques entre pausas, luego unir hasta target_len
    blocks = []
    for i in range(len(boundaries) - 1):
        blocks.append(words[boundaries[i]:boundaries[i + 1]])
    moments: list[dict] = []
    cur_start, cur_end, cur_words = None, None, []
    for block in blocks:
        first, last = block[0], block[-1]
        if cur_start is None:
            cur_start, cur_end, cur_words = first.start, last.end, list(block)
            continue
        if last.end - cur_start > max_len:
            moments.append(_moment(cur_start, cur_end, cur_words))
            cur_start, cur_end, cur_words = first.start, last.end, list(block)
        else:
            cur_end = last.end
            cur_words += list(block)
    if cur_start is not None and cur_end - cur_start >= 5.0:
        moments.append(_moment(cur_start, cur_end, cur_words))
    # Fusionar momentos demasiado cortos con el siguiente
    merged: list[dict] = []
    for mo in moments:
        if merged and (mo["end"] - mo["start"]) < min_len:
            prev = merged[-1]
            if mo["end"] - prev["start"] <= max_len:
                prev["end"], prev["text"] = mo["end"], prev["text"] + " " + mo["text"]
                continue
        merged.append(mo)
    # Energía de audio (opcional): anotar pico por momento
    if audio_energy:
        for mo in merged:
            lo, hi = int(mo["start"]), min(len(audio_energy), int(mo["end"]) + 1)
            mo["energy_peak"] = max(audio_energy[lo:hi]) if hi > lo else 0.0
    return merged


def _moment(start: float, end: float, words: list[Word]) -> dict:
    text = " ".join(w.text for w in words)
    return {"start": round(start, 2), "end": round(end, 2), "text": text}


# ---------------------------------------------------------------- puntuación 0-100
HOOK_WORDS = {"mira", "escucha", "atención", "nadie", "nunca", "siempre", "secreto",
              "verdad", "mentira", "peligro", "cuidado", "increíble", "imposible",
              "nadie te", "no vas a creer", "quédate", "hasta el final", "entonces",
              "de repente", "¿quién", "¿por qué", "¿qué"}
EMOTION_WORDS = {"miedo", "terror", "susto", "llora", "ríe", "risa", "grita", "grito",
                 "enfado", "rabia", "odio", "amor", "sorpresa", "impacto", "locura",
                 "increíble", "brutal", "triste", "feliz", "nervios", "tiembla"}
CURIOSITY_WORDS = {"pero", "sin embargo", "aunque", "entonces", "después", "al final",
                   "resulta", "descubrió", "oculto", "escondido", "misterio", "nadie sabe"}

CRITERIA = [
    ("gancho", 0.20, "¿Empieza con algo que retiene en los primeros 3 s?"),
    ("emocion", 0.15, "¿Hay emoción intensa (miedo, sorpresa, risa, rabia)?"),
    ("entretenimiento", 0.15, "¿Hay giro, sorpresa o contraste?"),
    ("originalidad", 0.10, "¿Dice algo poco común o específico (números, nombres, lugares)?"),
    ("curiosidad", 0.15, "¿Deja una pregunta abierta o un misterio sin resolver?"),
    ("comentarios", 0.10, "¿Invita a opinar o genera debate (pregunta, polémica, regla)?"),
    ("comprension", 0.10, "¿Se entiende solo, sin contexto previo?"),
    ("calidad", 0.05, "¿Duración adecuada (30-60 s) y texto suficiente?"),
]

# Pesos por categoría (cada fila suma 1.0). NO son los mismos criterios
# para todo: cada tipo de directo valora cosas distintas.
# - gameplay_streamer: manda la emoción y el entretenimiento (jugadas,
#   persecuciones, victorias, sustos, reacciones) y los picos de energía.
# - podcast_entrevista / reality_casa: mandan la curiosidad y los
#   comentarios (discusiones, declaraciones, preguntas, humor, tensión,
#   interrupciones); la emoción pesa menos que en gameplay.
# - conversacion_reaccion: equilibrio emoción/entretenimiento/comentarios
#   (reacciones, humor, preguntas al público).
# - otros: pesos neutros = los CRITERIA originales.
CATEGORY_WEIGHTS: dict[str, dict[str, float]] = {
    "gameplay_streamer": {"gancho": 0.20, "emocion": 0.25, "entretenimiento": 0.25,
                          "originalidad": 0.10, "curiosidad": 0.05, "comentarios": 0.05,
                          "comprension": 0.05, "calidad": 0.05},
    "podcast_entrevista": {"gancho": 0.15, "emocion": 0.10, "entretenimiento": 0.05,
                           "originalidad": 0.10, "curiosidad": 0.25, "comentarios": 0.20,
                           "comprension": 0.10, "calidad": 0.05},
    "reality_casa": {"gancho": 0.10, "emocion": 0.20, "entretenimiento": 0.20,
                     "originalidad": 0.05, "curiosidad": 0.15, "comentarios": 0.20,
                     "comprension": 0.05, "calidad": 0.05},
    "conversacion_reaccion": {"gancho": 0.15, "emocion": 0.20, "entretenimiento": 0.20,
                              "originalidad": 0.05, "curiosidad": 0.15, "comentarios": 0.15,
                              "comprension": 0.05, "calidad": 0.05},
    "otros": {name: w for name, w, _ in CRITERIA},
}

# Bonus por categoría (máx. +10 puntos sobre el total 0-100), calculado
# sobre el texto del segmento:
# - gameplay: pico de energía alto (grito, jugada) ya sube emoción/
#   entretenimiento; aquí se refuerza si hay palabras de juego.
# - podcast/reality/conversación: pregunta o frase cortada (interrupción
#   aparente: termina sin puntuación final o empieza por "pero"/"no, no").
GAMEPLAY_BONUS_WORDS = {"vamos", "nooo", "increible", "brutal", "victoria",
                        "gana", "corre", "cuidado", "mira", "gol", "kill"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def score_moment(segment: dict, energy_peak: float | None = None,
                 category: str = "otros") -> dict:
    """Puntuación 0-100 con desglose explicado por criterio. Determinista,
    calculada solo a partir del texto/energía del segmento.

    `category` cambia los PESOS (ver CATEGORY_WEIGHTS) y añade un bonus
    propio de la categoría (jugadas/reacciones en gameplay; preguntas e
    interrupciones aparentes en podcast/reality/conversación). Sin
    categoría se usan los pesos neutros ('otros')."""
    text = segment.get("text", "")
    norm = _norm(text)
    words = norm.split()
    n = max(len(words), 1)
    dur = max(segment.get("end", 0) - segment.get("start", 0), 1.0)
    first = " ".join(words[:8])
    scores: dict[str, float] = {}
    scores["gancho"] = min(100.0, 40 + 15 * sum(w in first for w in HOOK_WORDS)
                           + (20 if "?" in text else 0) + (10 if text.strip().endswith(("!", "?")) else 0))
    emo = sum(1 for w in EMOTION_WORDS if w in norm)
    scores["emocion"] = min(100.0, emo * 25 + (15 if energy_peak and energy_peak > 0.7 else 0))
    scores["entretenimiento"] = min(100.0, 30 + 20 * sum(w in norm for w in ("de repente", "entonces", "pero"))
                                    + (15 if energy_peak and energy_peak > 0.6 else 0))
    has_number = bool(re.search(r"\d", text))
    has_proper = bool(re.search(r"\b[A-ZÁÉÍÓÚ][a-záéíóú]{2,}\b", text))
    scores["originalidad"] = 30 + (35 if has_number else 0) + (35 if has_proper else 0)
    scores["curiosidad"] = min(100.0, 25 + 20 * sum(w in norm for w in CURIOSITY_WORDS) + (20 if "?" in text else 0))
    scores["comentarios"] = min(100.0, 20 + (40 if "?" in text else 0)
                                + (25 if any(w in norm for w in ("regla", "nunca", "siempre", "prohibido")) else 0))
    # Comprensión: frases completas y longitud razonable
    scores["comprension"] = min(100.0, 40 + min(40, n) + (20 if text.strip().endswith((".", "!", "?")) else 0))
    scores["calidad"] = 100.0 if 30 <= dur <= 60 else (70.0 if 15 <= dur <= 90 else 40.0)
    weights = CATEGORY_WEIGHTS.get(category, CATEGORY_WEIGHTS["otros"])
    total = sum(scores[name] * weights[name] for name, _, _ in CRITERIA)
    # Bonus propio de la categoría (máx. +10, documentado arriba).
    bonus = 0.0
    if category == "gameplay_streamer":
        if energy_peak and energy_peak > 0.7:
            bonus += 5.0
        if any(w in norm for w in GAMEPLAY_BONUS_WORDS):
            bonus += 5.0
    elif category in ("podcast_entrevista", "reality_casa", "conversacion_reaccion"):
        stripped = text.strip()
        # Interrupción aparente: frase cortada (sin puntuación final) o
        # arranque de réplica ("pero…", "no, no…"). Sin diarización real
        # es una aproximación, no una certeza.
        if stripped and not stripped.endswith((".", "!", "?", "…")):
            bonus += 5.0
        if "?" in text or norm.startswith(("pero", "no, no", "espera", "oye")):
            bonus += 5.0
    total = min(100.0, total + bonus)
    breakdown = {name: {"score": round(scores[name], 1), "weight": weights[name], "why": why}
                 for name, _, why in CRITERIA}
    return {"score": round(total, 1), "breakdown": breakdown, "category": category,
            "category_bonus": round(bonus, 1),
            "explanation": "; ".join(f"{n}: {scores[n]:.0f}/100" for n, _, _ in CRITERIA)}


# ---------------------------------------------------------------- títulos
_TITLE_TEMPLATES = [
    ("curiosidad", "Nadie te contó esto: {key}"),
    ("clickbait", "Esto pasó y nadie lo puede explicar: {key}"),
    ("humor", "Cuando {key}… no vas a creer lo que pasa después"),
    ("polemica", "La regla que nadie cumple: {key}"),
    ("directo", "{key}"),
]


def _key_phrase(text: str) -> str:
    """Frase clave: primera frase con contenido, recortada, sin inventar nada."""
    sents = [s.strip() for s in re.split(r"[.!?]\s+", text.strip()) if s.strip()]
    if not sents:
        return text.strip()[:60]
    best = max(sents, key=len)
    # Prefiere la frase con número o nombre propio
    for s in sents:
        if re.search(r"\d", s) or re.search(r"\b[A-ZÁÉÍÓÚ][a-záéíóú]{2,}\b", s):
            best = s
            break
    best = best[0].upper() + best[1:] if best else best
    return best[:80].rstrip(".,")


def generate_titles(transcript_text: str, count: int = 5) -> list[str]:
    """Al menos 5 títulos por reglas a partir del transcript REAL.
    Plantillas clickbait/curiosidad/humor/polémica; nada inventado fuera
    del texto. El LLM open source (Qwen3) queda como fase posterior, PENDIENTE."""
    key = _key_phrase(transcript_text)
    titles = []
    for kind, tpl in _TITLE_TEMPLATES:
        t = tpl.format(key=key)
        if t not in titles:
            titles.append(t)
        if len(titles) >= count:
            break
    # Relleno con variantes reales del texto si faltan
    sents = [s.strip() for s in re.split(r"[.!?]\s+", transcript_text.strip()) if len(s.strip()) > 12]
    i = 0
    while len(titles) < count and i < len(sents):
        cand = sents[i][0].upper() + sents[i][1:]
        if cand not in titles:
            titles.append(cand[:90])
        i += 1
    return titles[:max(count, 5)]


# ---------------------------------------------------------------- subtítulos ASS + render
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,64,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,0,2,60,60,180,1
Style: Keyword,DejaVu Sans,72,&H0000D4FF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,0,2,60,60,180,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_time(t: float) -> str:
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def build_ass(subtitles: list[dict], out_path: str, keyword: str = "",
                margin_v: int = 180) -> str:
    """subtitles: [{start, end, text}]. La palabra clave va en estilo Keyword
    (amarilla, más grande), resto en Default. Tiempos relativos al clip.
    `margin_v` desplaza los subtítulos desde el borde inferior (180 por
    defecto; 660 cuando el pane de facecam ocupa la parte inferior)."""
    header = ASS_HEADER
    if margin_v != 180:
        header = header.replace(",60,60,180,1", f",60,60,{margin_v},1")
    lines = [header]
    for sub in subtitles:
        text = sub["text"].replace("\n", " ")
        if keyword and keyword.lower() in text.lower():
            # Parte el texto: antes / keyword / después
            idx = text.lower().index(keyword.lower())
            before, kw, after = text[:idx], text[idx:idx + len(keyword)], text[idx + len(keyword):]
            text = f"{before}{{\\rKeyword}}{kw}{{\\rDefault}}{after}"
        lines.append(f"Dialogue: 0,{_ass_time(sub['start'])},{_ass_time(sub['end'])},Default,,0,0,0,,{text}")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return out_path


def subtitles_from_segments(segments: list[TranscriptSegment], clip_start: float, clip_end: float) -> list[dict]:
    """Subtítulos del clip: palabras agrupadas en líneas de ≤6 palabras,
    tiempos relativos al inicio del clip, sin cortar palabras. Una línea
    también se corta en una pausa > 0,8 s entre palabras (misma frontera
    que `detect_moments`): así un subtítulo nunca abarca un silencio
    largo. Regla portada del script e2e `e2e/work/make_ass.py` (que
    agrupaba por ≤38 caracteres o pausa > 0,8 s); el límite de 38
    caracteres no se porta: el tope de 6 palabras ya acota la línea y
    el camino dinámico (`worker/kinetic_subs.chunk_words`, que además
    corta en pausas > 0,5 s) es el usado por defecto en `process_job`."""
    subs: list[dict] = []
    for seg in segments:
        words = [w for w in seg.words if w.end > clip_start and w.start < clip_end]
        if not words and seg.end > clip_start and seg.start < clip_end:
            subs.append({"start": max(seg.start, clip_start) - clip_start,
                         "end": min(seg.end, clip_end) - clip_start, "text": seg.text})
            continue
        chunk: list[Word] = []
        for w in words:
            if chunk and (len(chunk) >= 6 or w.start - chunk[-1].end > 0.8):
                subs.append({"start": chunk[0].start - clip_start, "end": chunk[-1].end - clip_start,
                             "text": " ".join(x.text for x in chunk)})
                chunk = []
            chunk.append(w)
        if chunk:
            subs.append({"start": chunk[0].start - clip_start, "end": chunk[-1].end - clip_start,
                         "text": " ".join(x.text for x in chunk)})
    return subs


def _probe_size(path: str) -> tuple[int, int]:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        text=True).strip().split(",")
    return int(out[0]), int(out[1])


def _even(n: int) -> int:
    return n - (n % 2)


def _face_region(face_rel: tuple[float, float, float, float], src_w: int, src_h: int,
                 width_mult: float, aspect: float) -> tuple[int, int, int, int]:
    """Región de recorte alrededor de un rostro (coords relativas 0..1):
    ancho = `width_mult` x ancho del rostro, alto según `aspect` (w/h),
    centrada en el rostro y ajustada a los bordes del frame. Devuelve
    (x, y, w, h) en píxeles pares (exigencia de yuv420p)."""
    fx, fy, fw, fh = face_rel
    fcx, fcy = (fx + fw / 2.0) * src_w, (fy + fh / 2.0) * src_h
    rw = _even(min(src_w, max(64, int(fw * src_w * width_mult))))
    rh = _even(min(src_h, max(64, int(rw / aspect))))
    rx = _even(min(max(int(fcx - rw / 2), 0), src_w - rw))
    ry = _even(min(max(int(fcy - rh / 2), 0), src_h - rh))
    return rx, ry, rw, rh


def _effective_layout(layout: str, facecam: dict | None) -> str:
    """Layout efectivo tras aplicar las reglas de fallback (compartidas
    por el camino estático y el dinámico): sin `facecam` válida no hay
    composición con facecam; sin rostros no hay talking_head/multi_window.
    El fallback es explícito y documentado, nunca silencioso en el
    sentido de fallar: el render cae a 'gameplay_only' (o a 'facecam_top'
    si multi_window tiene una sola cara válida) y sigue."""
    fc = facecam if isinstance(facecam, dict) else {}
    fc_bbox = fc.get("bbox")
    faces = fc.get("faces", []) or []
    if layout in ("facecam_top", "facecam_bottom") and not fc_bbox:
        return "gameplay_only"
    if layout == "talking_head" and not (fc.get("face_bbox") or faces):
        return "gameplay_only"
    if layout == "multi_window" and len(faces) < 2:
        return "facecam_top" if fc_bbox else "gameplay_only"
    return layout


def _facecam_pane_filter(fc_bbox: tuple[float, float, float, float],
                         src_w: int, src_h: int) -> str:
    """Rama de la facecam: recorte de su caja (coords relativas 0..1) y
    escalado al pane 1080x480 con bandas negras si la caja no es 9:4."""
    fx, fy, fw, fh = fc_bbox
    cx0 = _even(int(fx * src_w))
    cy0 = _even(int(fy * src_h))
    cw = _even(max(2, int(fw * src_w)))
    ch = _even(max(2, int(fh * src_h)))
    cw, ch = min(cw, src_w - cx0), min(ch, src_h - cy0)
    cw, ch = _even(cw), _even(ch)
    return (f"crop={cw}:{ch}:{cx0}:{cy0},"
            "scale=1080:480:force_original_aspect_ratio=decrease,"
            "pad=1080:480:(ow-iw)/2:(oh-ih)/2:black")


def _zoom_suffix(windows: list[tuple[float, float]], out_w: int = 1080,
                 out_h: int = 1920) -> str:
    """Sufijo de crop animado (zoom de sorpresa) + reescalado al tamaño
    de salida. Vacío si no hay ventanas de sorpresa."""
    if not windows:
        return ""
    z = zoom_crop_expression(windows)
    return (f",crop=iw*({z}):ih*({z}):(iw-iw*({z}))/2:(ih-ih*({z}))/2,"
            f"scale={out_w}:{out_h}")


def _dynamic_base(category: str, focus_x: float | None) -> str:
    """Base del render dinámico gameplay_only: escalar para cubrir
    1080x1920 y recortar. Con foco en rostro (conversación/podcast/
    reality) la ventana se desplaza hacia focus_x, como en el estático;
    en gameplay y 'otros' el recorte es centrado."""
    if (category in ("podcast_entrevista", "reality_casa", "conversacion_reaccion")
            and focus_x is not None):
        crop_x = f"(in_w-1080)*{max(0.0, min(1.0, float(focus_x))):.2f}"
    else:
        crop_x = "(in_w-1080)/2"
    return ("scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920:x='{crop_x}':y='(in_h-1920)/2'")


def _dynamic_compose_filter(layout: str, facecam: dict, src_w: int, src_h: int,
                            windows: list[tuple[float, float]], ass_path: str,
                            src_label: str) -> str:
    """Filtro de composición para el render dinámico con layout.

    - facecam_top/bottom: dos ramas desde `src_label` (gameplay + facecam,
      vstack); el zoom cinético se aplica SOLO sobre la rama de gameplay
      (pane 1080x1440), nunca sobre la facecam, que queda fija.
    - multi_window: dos recortes de rostro apilados (1080x960 cada uno);
      el zoom se aplica sobre el lienzo ya compuesto (ambos panes a la
      vez: no hay un "gameplay" que mandar).
    Devuelve el filter_complex terminado en `[vout]` (con los subtítulos
    cinéticos ya aplicados). `src_label` es "0:v" (sin recorte de
    silencios) o "v" (salida del concat tras recortar silencios).
    """
    fc = facecam if isinstance(facecam, dict) else {}
    if layout == "multi_window":
        faces = (fc.get("faces", []) or [])[:2]
        panes = []
        for face in faces:
            rx, ry, rw, rh = _face_region(face["bbox"], src_w, src_h,
                                          width_mult=4.0, aspect=1080 / 960)
            panes.append(f"crop={rw}:{rh}:{rx}:{ry},"
                         "scale=1080:960:force_original_aspect_ratio=increase,"
                         "crop=1080:960")
        return (f"[{src_label}]split=2[a][b];[a]{panes[0]}[p0];[b]{panes[1]}[p1];"
                f"[p0][p1]vstack=inputs=2{_zoom_suffix(windows)},"
                f"subtitles='{ass_path}'[vout]")
    gp = ("scale=1080:1440:force_original_aspect_ratio=increase,"
          "crop=1080:1440" + _zoom_suffix(windows, 1080, 1440))
    fc_pane = _facecam_pane_filter(fc["bbox"], src_w, src_h)
    order = "[fc2][gp2]" if layout == "facecam_top" else "[gp2][fc2]"
    return (f"[{src_label}]split=2[gp][fc];[gp]{gp}[gp2];[fc]{fc_pane}[fc2];"
            f"{order}vstack=inputs=2,subtitles='{ass_path}'[vout]")


def render_clip(source: str, start: float, end: float, subtitles: list[dict],
                out_path: str, keyword: str = "", style: str = "static",
                words: list[Word] | None = None,
                subtitle_position: str = "bottom",
                category: str = "otros", focus_x: float | None = None,
                layout: str = "gameplay_only",
                facecam: dict | None = None) -> str:
    """MP4 vertical 9:16 1080x1920 H.264 + AAC, subtítulos ASS quemados.
    Recorte centrado: crop al centro del frame (detección de movimiento
    simple no implementada; el seguimiento de rostros queda PENDIENTE).
    El crop usa scale+crop: se escala para cubrir 1080x1920 y se recorta
    el centro horizontal/vertical.

    style="static" (defecto): ASS estático actual, palabra clave amarilla.
    style="dynamic": motor cinético (worker/kinetic_subs.py) — requiere
    `words` (lista de Word con timestamps reales de faster-whisper, en
    tiempo absoluto del origen). Sincroniza cada palabra, resalta la
    palabra clave / de impacto, aplica zoom de sorpresa y recorta pausas
    largas (>1,5 s) sin cortar palabras. `subtitle_position` es "bottom"
    o "raised" (sube los subtítulos si la zona inferior es importante).

    `category` + `focus_x` (clasificación, ver worker/classify.py): en
    conversación/podcast/reality con `focus_x` (0..1, posición horizontal
    del rostro que habla) el recorte se desplaza hacia esa persona en vez
    del centro, en el camino estático Y en el dinámico (gameplay_only);
    en gameplay y 'otros' el recorte es centrado.

    `layout` + `facecam` (composición, ver worker/vision.py):
    `layout` es 'gameplay_only' (defecto, comportamiento v1),
    'facecam_top' (facecam arriba 1080x480 + gameplay abajo 1080x1440),
    'facecam_bottom' (gameplay arriba + facecam abajo; los subtítulos se
    desplazan a MarginV 660 en estático / posición "raised", MarginV 720,
    en dinámico, para no tapar la facecam),
    'talking_head' (recorte centrado en el rostro) o 'multi_window'
    (dos rostros estables apilados, 1080x960 cada uno). `facecam` es el
    dict de ``detect_facecam``. Sin `facecam` válida el layout cae a
    'gameplay_only' (ver `_effective_layout`).

    Layout + style dinámico (unificado): el render dinámico respeta el
    layout. En facecam_top/bottom el zoom cinético se aplica solo sobre
    la rama de gameplay y la facecam queda fija; en talking_head se
    aplica el recorte de rostro + subtítulos cinéticos (+ zoom sobre el
    recorte); en multi_window, recortes de rostro apilados + subtítulos
    cinéticos (el zoom, si lo hay, actúa sobre el lienzo compuesto).
    Fallbacks (documentados en `_effective_layout`, nunca un fallo
    silencioso distinto del render): sin bbox de facecam -> gameplay_only
    dinámico; talking_head sin rostro -> gameplay_only dinámico;
    multi_window con <2 rostros -> facecam_top dinámico si hay bbox, si
    no gameplay_only dinámico.
    """
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg no disponible")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    dur = end - start
    if layout not in ("gameplay_only", "facecam_top", "facecam_bottom",
                      "talking_head", "multi_window"):
        raise ValueError(f"layout desconocido: {layout}")
    layout = _effective_layout(layout, facecam)
    if style == "dynamic":
        if not words:
            raise ValueError("render dinámico necesita `words` con timestamps "
                             "por palabra (faster-whisper); sin ellos no se "
                             "puede sincronizar y no se inventan tiempos")
        # Facecam abajo: los subtítulos cinéticos suben ("raised",
        # MarginV 720) para no tapar el pane de facecam (y 1440-1920).
        if layout == "facecam_bottom":
            subtitle_position = "raised"
        return _render_clip_dynamic(source, start, end, words, out_path,
                                    keyword=keyword,
                                    subtitle_position=subtitle_position,
                                    layout=layout, facecam=facecam,
                                    category=category, focus_x=focus_x)
    # Composición con facecam: sin detección válida, cae a gameplay_only.
    fc = facecam if isinstance(facecam, dict) else {}
    fc_bbox = fc.get("bbox")
    faces = fc.get("faces", []) or []
    margin_v = 660 if layout == "facecam_bottom" else 180
    ass_path = str(Path(out_path).with_suffix(".ass"))
    build_ass(subtitles, ass_path, keyword=keyword, margin_v=margin_v)
    sub_filter = f"subtitles='{ass_path}':force_style='Fontsize=64'"
    base = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", source,
            "-t", str(dur)]
    common = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_path]
    if layout == "gameplay_only":
        # Escalar para cubrir 1080x1920 y recortar. Con foco en rostro
        # (conversación/podcast/reality) la ventana se desplaza hacia focus_x.
        if (category in ("podcast_entrevista", "reality_casa", "conversacion_reaccion")
                and focus_x is not None):
            crop_x = f"(in_w-1080)*{max(0.0, min(1.0, float(focus_x))):.2f}"
        else:
            crop_x = "(in_w-1080)/2"
        vf = ("scale=1080:1920:force_original_aspect_ratio=increase,"
              f"crop=1080:1920:x='{crop_x}':y='(in_h-1920)/2',"
              f"{sub_filter}")
        run(base + ["-vf", vf] + common)
        return out_path
    src_w, src_h = _probe_size(source)
    if layout == "talking_head":
        face_rel = fc.get("face_bbox") or faces[0]["bbox"]
        rx, ry, rw, rh = _face_region(face_rel, src_w, src_h,
                                      width_mult=4.0, aspect=9 / 16)
        vf = f"crop={rw}:{rh}:{rx}:{ry},scale=1080:1920,{sub_filter}"
        run(base + ["-vf", vf] + common)
        return out_path
    # facecam_top / facecam_bottom / multi_window: filter_complex, 2 ramas.
    if layout == "multi_window":
        panes = []
        for face in faces[:2]:
            rx, ry, rw, rh = _face_region(face["bbox"], src_w, src_h,
                                          width_mult=4.0, aspect=1080 / 960)
            panes.append(f"crop={rw}:{rh}:{rx}:{ry},"
                         "scale=1080:960:force_original_aspect_ratio=increase,"
                         "crop=1080:960")
        fcplx = (f"[0:v]split=2[a][b];[a]{panes[0]}[p0];[b]{panes[1]}[p1];"
                 f"[p0][p1]vstack=inputs=2,{sub_filter}[v]")
    else:
        gp = ("scale=1080:1440:force_original_aspect_ratio=increase,"
              "crop=1080:1440")
        fc_pane = _facecam_pane_filter(fc_bbox, src_w, src_h)
        order = "[fc2][gp2]" if layout == "facecam_top" else "[gp2][fc2]"
        fcplx = (f"[0:v]split=2[gp][fc];[gp]{gp}[gp2];[fc]{fc_pane}[fc2];"
                 f"{order}vstack=inputs=2,{sub_filter}[v]")
    run(base + ["-filter_complex", fcplx, "-map", "[v]", "-map", "0:a"] + common)
    return out_path


def _render_clip_dynamic(source: str, start: float, end: float, words: list[Word],
                         out_path: str, keyword: str = "",
                         subtitle_position: str = "bottom",
                         layout: str = "gameplay_only",
                         facecam: dict | None = None,
                         category: str = "otros",
                         focus_x: float | None = None) -> str:
    """Render con subtítulos cinéticos (ver worker/kinetic_subs.py),
    respetando `layout` (ver `render_clip` y `_dynamic_compose_filter`).

    - gameplay_only: comportamiento v1 intacto (base con/sin focus_x +
      zoom cinético + ASS cinético), salvo que ahora `focus_x` sí se
      aplica en conversación/podcast/reality, como en el estático.
    - facecam_top/bottom: composición de dos ramas; el zoom cinético va
      solo sobre el gameplay (1080x1440); la facecam queda fija.
    - talking_head: recorte de rostro + zoom + ASS cinético.
    - multi_window: dos recortes de rostro apilados + ASS cinético.
    Con recorte de silencios, el trim/atrim+concat se hace sobre el
    vídeo de origen (a resolución original, para que los recortes de
    rostro/facecam usen coords de origen) y la composición/zoom se aplica
    DESPUÉS del concat, porque las ventanas de zoom están en tiempo
    recortado. Sin recorte, gameplay_only y talking_head usan -vf simple
    y los layouts compuestos usan filter_complex.
    """
    from worker.kinetic_subs import (
        build_kinetic_ass, remap_words, silence_plan, zoom_windows,
    )

    layout = _effective_layout(layout, facecam)
    if layout == "facecam_bottom":
        subtitle_position = "raised"
    clip_words = [w for w in words if w.end > start and w.start < end]
    if not clip_words:
        raise ValueError("no hay palabras con timestamp dentro del clip")
    plan = silence_plan(words, start, end)
    use_trim = plan.total_removed > 0.05 and len(plan.keep) > 1
    eff_words = remap_words(clip_words, plan, start) if use_trim else clip_words
    ass_text, effects = build_kinetic_ass(eff_words, clip_start=0.0 if use_trim else start,
                                          keyword=keyword, position=subtitle_position)
    ass_path = str(Path(out_path).with_suffix(".ass"))
    Path(ass_path).write_text(ass_text, encoding="utf-8")
    windows = zoom_windows(effects)
    zoom = _zoom_suffix(windows)
    # Base: escalar para cubrir 1080x1920 y recortar (con focus_x en
    # conversación, como el estático).
    base = _dynamic_base(category, focus_x)
    dur = (end - start) - (plan.total_removed if use_trim else 0.0)
    common = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_path]
    composed = layout in ("facecam_top", "facecam_bottom", "multi_window")
    src_w = src_h = None
    if composed or layout == "talking_head":
        src_w, src_h = _probe_size(source)
    if not use_trim:
        if layout == "gameplay_only":
            vf = f"{base}{zoom},subtitles='{ass_path}'"
            run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", source, "-t", str(dur),
                 "-vf", vf] + common)
            return out_path
        if layout == "talking_head":
            fc = facecam if isinstance(facecam, dict) else {}
            faces = fc.get("faces", []) or []
            face_rel = fc.get("face_bbox") or faces[0]["bbox"]
            rx, ry, rw, rh = _face_region(face_rel, src_w, src_h,
                                          width_mult=4.0, aspect=9 / 16)
            vf = (f"crop={rw}:{rh}:{rx}:{ry},scale=1080:1920{zoom},"
                  f"subtitles='{ass_path}'")
            run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", source, "-t", str(dur),
                 "-vf", vf] + common)
            return out_path
        fcplx = _dynamic_compose_filter(layout, facecam, src_w, src_h,
                                        windows, ass_path, "0:v")
        run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", source, "-t", str(dur),
             "-filter_complex", fcplx, "-map", "[vout]", "-map", "0:a"] + common)
        return out_path
    # Con recorte de silencios: trim/atrim por intervalo + concat, con los
    # subtítulos ya remapeados al tiempo recortado. El zoom se aplica
    # DESPUÉS del concat, porque sus ventanas están en tiempo recortado.
    # (split/asplit: cada etiqueta solo se puede consumir una vez.)
    # En gameplay_only el trim va sobre el frame ya escalado (v1); en los
    # demás layouts va sobre el origen, para que los recortes de
    # rostro/facecam usen coordenadas de origen.
    n = len(plan.keep)
    vs = "".join(f"[vs{i}]" for i in range(n))
    ains = "".join(f"[as{i}]" for i in range(n))
    if layout == "gameplay_only":
        parts = [f"[0:v]{base},split={n}{vs};",
                 f"[0:a]asplit={n}{ains};"]
    else:
        parts = [f"[0:v]split={n}{vs};",
                 f"[0:a]asplit={n}{ains};"]
    vlabels, alabels = [], []
    for i, (s, e) in enumerate(plan.keep):
        parts.append(f"[vs{i}]trim=start={s:.2f}:end={e:.2f},setpts=PTS-STARTPTS[v{i}];")
        parts.append(f"[as{i}]atrim=start={s:.2f}:end={e:.2f},asetpts=PTS-STARTPTS[a{i}];")
        vlabels.append(f"[v{i}]")
        alabels.append(f"[a{i}]")
    # concat espera los pares intercalados: [v0][a0][v1][a1]...
    interleaved = "".join(f"[v{i}][a{i}]" for i in range(n))
    if layout == "gameplay_only":
        fc = ("".join(parts) +
              f"{interleaved}concat=n={n}:v=1:a=1[v][a];"
              f"[v]{zoom.lstrip(',') + ',' if zoom else ''}subtitles='{ass_path}'[vout]")
    elif layout == "talking_head":
        fc_dict = facecam if isinstance(facecam, dict) else {}
        faces = fc_dict.get("faces", []) or []
        face_rel = fc_dict.get("face_bbox") or faces[0]["bbox"]
        rx, ry, rw, rh = _face_region(face_rel, src_w, src_h,
                                      width_mult=4.0, aspect=9 / 16)
        fc = ("".join(parts) +
              f"{interleaved}concat=n={n}:v=1:a=1[v][a];"
              f"[v]crop={rw}:{rh}:{rx}:{ry},scale=1080:1920"
              f"{zoom},subtitles='{ass_path}'[vout]")
    else:
        fc = ("".join(parts) +
              f"{interleaved}concat=n={n}:v=1:a=1[v][a];" +
              _dynamic_compose_filter(layout, facecam, src_w, src_h,
                                      windows, ass_path, "v"))
    run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", source, "-t", str(end - start),
         "-filter_complex", fc, "-map", "[vout]", "-map", "[a]"] + common)
    return out_path


def make_thumbnail(source: str, at: float, out_path: str) -> str:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error", "-ss", str(at), "-i", source,
         "-frames:v", "1", "-vf", "scale=540:960", out_path])
    return out_path
