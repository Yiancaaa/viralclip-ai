"""Motor de subtítulos cinéticos de VIRALCLIP AI (0 $, open source + FFmpeg).

Idea: a partir de los timestamps por palabra de faster-whisper
(``TranscriptSegment.words``) genera un ASS donde cada palabra aparece
sincronizada con el audio, la palabra actual se resalta (color + tamaño +
pop con ``\\t``) y las anteriores quedan en blanco. Los bloques son de
<=6 palabras para que quepan en una línea legible a 1080x1920.

Edición inteligente (no se anima todo): cada bloque se clasifica y solo
recibe el efecto que le corresponde:

* ``impact``   — exclamación / grito / palabra de impacto-emoción
  ("!", mayúsculas, "grita", "terror", "ya te vio", "lo recordará",
  "un clic", ...) -> palabra en rosa/rojo, mucho más grande, pop fuerte.
* ``surprise`` — sorpresa ("de repente", "qué", "?", "¿quién", "entonces")
  -> zoom breve del clip (ventana devuelta en ``zoom_windows``; el render
  la aplica con un crop animado) + palabra en amarillo.
* ``highlight``— chiste / frase clave: palabra clave del momento, "Regla N",
  números o nombres propios -> frase destacada en amarillo.
* ``tension``  — tensión ("silencio", "oscuro", "tiembla", "respiran", ...)
  -> ritmo visual progresivo: cada palabra aparece un poco más grande
  que la anterior.
* ``normal``   — resto: aparición palabra a palabra, sin efectos extra.

Silencios: ``silence_plan`` detecta gaps entre palabras > ``max_gap``
(por defecto 1,5 s) y devuelve los intervalos a conservar + el remapeo de
tiempos; el render dinámico recorta esas pausas sin cortar palabras.

No se inventan palabras ni se corrige el transcript: todo sale de los
``Word`` reales. La fuente es DejaVu Sans Bold (disponible en el sistema,
verificado con fc-list); nada de fuentes de pago.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ------------------------------------------------------------------ constantes
FONT = "DejaVu Sans Bold"
BASE_FONTSIZE = 72
KEYWORD_FONTSIZE = 84
IMPACT_FONTSIZE = 96
MAX_WORDS_PER_BLOCK = 6
# Colores ASS en formato &HAABBGGRR
WHITE = "&H00FFFFFF"
YELLOW = "&H0000D4FF"   # amarillo VIRALCLIP (mismo que el estilo estático)
PINK = "&H006040FF"     # rosa/rojo de impacto (RGB 255,64,96)
ORANGE = "&H000080FF"   # naranja de tensión (RGB 255,128,0)
GREY = "&H00C8C8C8"     # palabras ya dichas, atenuadas

IMPACT_WORDS = {
    "grita", "grito", "terror", "miedo", "brutal", "increible", "locura",
    "impacto", "sorpresa", "odio", "rabia", "enfado", "peligro", "cuidado",
    "nunca", "siempre", "nadie", "clic", "recor", "recordara",
}
# Frases de impacto (normalizadas, sin tildes) buscadas en el texto del bloque
IMPACT_PHRASES = {
    "ya te vio", "lo recordara", "un clic", "no salgas", "no mires",
    "no digas", "no enciendas", "no entres",
}
SURPRISE_WORDS = {"de", "repente", "que", "quien", "entonces", "como"}
SURPRISE_PHRASES = {"de repente", "que paso", "quien", "entonces"}
TENSION_WORDS = {
    "silencio", "oscuro", "oscuridad", "tiembla", "respiran", "respira",
    "vacio", "vacía", "vacia", "noche", "sola", "solo", "duerme", "duermiendo",
    "misterio", "oculto", "escondido",
}
HIGHLIGHT_MARKERS = re.compile(r"\bregla\s+\d+\b|\bregla\s+(uno|dos|tres)\b", re.I)
# Números en palabra: en La casa los contadores ("siete ventanas, cero
# luces", "cuarenta pasos") son datos clave y van destacados.
NUMBER_WORDS = {"cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete",
                "ocho", "nueve", "diez", "once", "doce", "veinte", "treinta",
                "cuarenta", "cincuenta", "cien", "mil"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _ass_time(t: float) -> str:
    h, rem = divmod(max(t, 0.0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


# ------------------------------------------------------------------ clasificación
@dataclass
class BlockEffect:
    """Efecto elegido para un bloque de palabras."""
    kind: str  # impact | surprise | highlight | tension | normal
    reason: str
    # Índices (dentro del bloque) de las palabras a las que aplica el efecto
    word_idx: list[int] = field(default_factory=list)


def classify_block(words: list, keyword: str = "") -> BlockEffect:
    """Clasifica un bloque (lista de Word con .text) y elige UN efecto.

    Prioridad: impact > surprise > highlight > tension > normal.
    Solo se anima lo que el contenido pide; el resto queda en 'normal'.
    """
    text = " ".join(w.text for w in words)
    norm = _norm(text)
    norm_words = norm.split()
    stripped = [re.sub(r"[^\w]", "", w) for w in norm_words]

    # --- impact: "!", palabra en mayúsculas, palabra/frase de impacto
    impact_idx = [i for i, w in enumerate(words)
                  if re.sub(r"[^\w]", "", _norm(w.text)) in IMPACT_WORDS]
    if "!" in text:
        impact_idx = list(range(len(words))) if not impact_idx else impact_idx
    if any(p in norm for p in IMPACT_PHRASES):
        impact_idx = impact_idx or [len(words) - 1]
    if any(w.text.isupper() and len(w.text) > 1 for w in words):
        impact_idx = impact_idx or [i for i, w in enumerate(words) if w.text.isupper()]
    if impact_idx:
        return BlockEffect("impact", "palabra/exclamación de impacto en el transcript", impact_idx)

    # --- surprise: "?", "de repente", "qué/quién/entonces" interrogativo
    if "?" in text or "¿" in text or any(p in norm for p in SURPRISE_PHRASES):
        idx = [i for i, w in enumerate(words)
               if re.sub(r"[^\w]", "", _norm(w.text)) in SURPRISE_WORDS or "?" in w.text]
        return BlockEffect("surprise", "sorpresa/pregunta en el transcript", idx or [0])

    # --- highlight: palabra clave del momento, "Regla N", números, nombre propio
    hl_idx: list[int] = []
    if keyword:
        kw = _norm(keyword)
        hl_idx = [i for i, w in enumerate(words) if kw and kw in _norm(w.text)]
    if HIGHLIGHT_MARKERS.search(text):
        hl_idx = hl_idx or list(range(len(words)))
    if not hl_idx:
        hl_idx = [i for i, w in enumerate(words)
                  if re.search(r"\d", w.text)
                  or re.sub(r"[^\w]", "", _norm(w.text)) in NUMBER_WORDS
                  or (w.text[:1].isupper() and w.text[1:2].islower() and len(w.text) > 2)]
    if hl_idx:
        return BlockEffect("highlight", "frase/palabra clave del momento", hl_idx)

    # --- tension: palabras de tensión -> ritmo progresivo
    ten_idx = [i for i, w in enumerate(words)
               if re.sub(r"[^\w]", "", _norm(w.text)) in TENSION_WORDS]
    if ten_idx:
        return BlockEffect("tension", "palabras de tensión: ritmo progresivo", ten_idx)

    return BlockEffect("normal", "narración neutra: aparición palabra a palabra", [])


def chunk_words(words: list, max_words: int = MAX_WORDS_PER_BLOCK) -> list[list]:
    """Agrupa palabras en bloques de <= max_words, sin cortar palabras
    y cortando preferentemente en pausas (>0,5 s entre palabras)."""
    blocks: list[list] = []
    cur: list = []
    for w in words:
        if cur and (len(cur) >= max_words
                    or (w.start - cur[-1].end) >= 0.5):
            blocks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        blocks.append(cur)
    return blocks


# ------------------------------------------------------------------ ASS cinético
ASS_HEADER_KINETIC = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Kinetic,DejaVu Sans Bold,72,&H00FFFFFF,&H0000D4FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _word_render(word_text: str, effect: BlockEffect, idx_in_block: int,
                 block_len: int, is_current: bool, is_keyword: bool) -> str:
    """Render ASS de una palabra dentro de un evento acumulativo.

    La palabra actual va resaltada (color + tamaño + pop con \\t);
    las ya dichas quedan en blanco/gris atenuado.
    """
    text = _ass_escape(word_text)
    if not is_current:
        return f"{{\\c{GREY}&\\fs{BASE_FONTSIZE - 4}}}{text}"
    kind = effect.kind
    if kind == "impact" and idx_in_block in effect.word_idx:
        # Pop fuerte: entra al 130% y rebota a 100% en 180 ms
        return (f"{{\\c{PINK}&\\fs{IMPACT_FONTSIZE}"
                f"\\t(0,90,\\fscx130\\fscy130)\\t(90,220,\\fscx100\\fscy100)}}"
                f"{text}")
    if kind == "surprise" and idx_in_block in effect.word_idx:
        return (f"{{\\c{YELLOW}&\\fs{KEYWORD_FONTSIZE}"
                f"\\t(0,120,\\fscx125\\fscy125)\\t(120,260,\\fscx100\\fscy100)}}"
                f"{text}")
    if is_keyword or (kind == "highlight" and idx_in_block in effect.word_idx):
        return (f"{{\\c{YELLOW}&\\fs{KEYWORD_FONTSIZE}"
                f"\\t(0,100,\\fscx118\\fscy118)\\t(100,220,\\fscx100\\fscy100)}}"
                f"{text}")
    if kind == "tension":
        # Ritmo progresivo: cada palabra un poco más grande
        fs = BASE_FONTSIZE + int(10 * (idx_in_block + 1) / max(block_len, 1))
        return (f"{{\\c{ORANGE}&\\fs{fs}"
                f"\\t(0,100,\\fscx112\\fscy112)\\t(100,200,\\fscx100\\fscy100)}}"
                f"{text}")
    # Normal: pop suave de entrada
    return (f"{{\\c{WHITE}&\\fs{BASE_FONTSIZE}"
            f"\\t(0,80,\\fscx112\\fscy112)\\t(80,180,\\fscx100\\fscy100)}}"
            f"{text}")


def build_kinetic_ass(words: list, clip_start: float = 0.0, keyword: str = "",
                      position: str = "bottom") -> tuple[str, list[dict]]:
    """Genera el ASS cinético a partir de palabras con timestamps reales.

    Devuelve (texto ASS, efectos): efectos es una lista por bloque con
    ``{start, end, kind, reason, text}`` (tiempos relativos al clip).
    Cada palabra genera un evento acumulativo: el texto crece palabra a
    palabra y la palabra actual es la resaltada. El último evento de cada
    bloque se mantiene 0,25 s extra para que se pueda leer el bloque entero.
    """
    # Posición: "bottom" (MarginV 200, estándar para metraje sin texto
    # quemado) o "raised" (MarginV 720, justo por encima del tercio
    # inferior: para vídeos con texto/HUD quemado abajo, como La Casa,
    # cuyo texto gigante ocupa desde ~y=1230 hasta abajo).
    margin_v = 200 if position == "bottom" else 720
    lines = [ASS_HEADER_KINETIC.format(margin_v=margin_v)]
    effects: list[dict] = []
    rel_words = [w for w in words if w.end > clip_start]
    for block in chunk_words(rel_words):
        effect = classify_block(block, keyword=keyword)
        b_start = block[0].start - clip_start
        b_end = block[-1].end - clip_start
        effects.append({"start": round(b_start, 2), "end": round(b_end, 2),
                        "kind": effect.kind, "reason": effect.reason,
                        "text": " ".join(w.text for w in block)})
        for i, w in enumerate(block):
            ev_start = w.start - clip_start
            if i + 1 < len(block):
                ev_end = block[i + 1].start - clip_start
            else:
                ev_end = w.end - clip_start + 0.25
            # Evita eventos de duración ~0 (palabras pegadas): mínimo 0,12 s
            ev_end = max(ev_end, ev_start + 0.12)
            parts = []
            for j, pw in enumerate(block[: i + 1]):
                is_kw = bool(keyword) and _norm(keyword) in _norm(pw.text)
                parts.append(_word_render(pw.text, effect, j, len(block),
                                          is_current=(j == i), is_keyword=is_kw))
            text = " ".join(parts)
            # {\r} inicial para resetear estilo entre palabras
            lines.append(
                f"Dialogue: 0,{_ass_time(ev_start)},{_ass_time(ev_end)},"
                f"Kinetic,,0,0,0,,{{\\r}}{text}")
    return "\n".join(lines), effects


def zoom_windows(effects: list[dict], pad: float = 0.15) -> list[tuple[float, float]]:
    """Ventanas (relativas al clip) en las que aplicar el zoom de sorpresa."""
    return [(max(0.0, e["start"] - pad), e["end"] + pad)
            for e in effects if e["kind"] == "surprise"]


def zoom_crop_expression(windows: list[tuple[float, float]], strength: float = 0.10) -> str:
    """Expresión de crop para un zoom breve y suave en cada ventana.

    Devuelve la expresión para el ancho/alto del crop (fracción de iw/ih):
    fuera de las ventanas es 1 (sin zoom); dentro, baja hasta 1-strength
    en el centro de la ventana y vuelve a 1 (zoom de entrada y salida).
    Se evalúa por fotograma (variables t de ffmpeg).
    """
    if not windows:
        return "1"
    terms = []
    for (s, e) in windows:
        half = max((e - s) / 2.0, 0.05)
        # Triángulo suave con if(): fuera de la ventana vale 0 exacto.
        # (Obligatorio: en la configuración inicial ffmpeg evalúa con t=NaN
        # y cualquier expresión que propague NaN — abs(), min/max con t —
        # hace fallar el crop; if(between(...)) evalúa perezosamente.)
        terms.append(
            f"if(between(t\\,{s:.2f}\\,{e:.2f})\\,"
            f"min((t-{s:.2f})/{half:.2f}\\,({e:.2f}-t)/{half:.2f})\\,0)")
    joined = "+".join(terms)
    return f"(1-{strength:.2f}*min(1\\,max(0\\,{joined})))"


# ------------------------------------------------------------------ silencios
@dataclass
class SilencePlan:
    """Intervalos a conservar (relativos al clip) y remapeo de tiempos."""
    keep: list[tuple[float, float]]  # (start, end) relativos al clip
    removed_before: list[float]      # segundos eliminados antes de cada keep
    total_removed: float = 0.0

    def remap(self, t: float) -> float:
        """Tiempo original -> tiempo en el clip recortado."""
        out = t
        for (s, _e), removed in zip(self.keep, self.removed_before):
            if t >= s:
                out = t - removed
        return out


def silence_plan(words: list, clip_start: float, clip_end: float,
                 max_gap: float = 1.5, keep_gap: float = 0.4) -> SilencePlan:
    """Plan de recorte de pausas largas entre palabras.

    Un gap > max_gap se recorta dejando solo keep_gap segundos de pausa.
    Nunca corta palabras ni contexto: solo acorta el silencio entre ellas.
    Si no hay gaps largos, el plan es un único intervalo (sin recorte).
    """
    rel = sorted((w.start - clip_start, w.end - clip_start) for w in words
                 if w.end > clip_start and w.start < clip_end)
    if not rel:
        return SilencePlan(keep=[(0.0, clip_end - clip_start)], removed_before=[0.0])
    keep: list[tuple[float, float]] = []
    removed_before: list[float] = []
    removed_total = 0.0
    cur_start = 0.0
    prev_end = 0.0
    for (s, e) in rel:
        gap = s - prev_end
        if gap > max_gap and prev_end > 0:
            # Cerrar intervalo antes del gap, reabrir tras keep_gap
            keep.append((cur_start, prev_end + keep_gap))
            removed_before.append(removed_total)
            cut = gap - keep_gap
            removed_total += cut
            cur_start = s  # el nuevo intervalo empieza en la palabra;
            # la pausa conservada es el tramo prev_end..prev_end+keep_gap
        prev_end = max(prev_end, e)
    keep.append((cur_start, clip_end - clip_start))
    removed_before.append(removed_total)
    # Normalizar: los inicios de intervalo en tiempo original -> el plan
    # guarda intervalos originales; el render los remapea con removed_before.
    return SilencePlan(keep=keep, removed_before=removed_before,
                       total_removed=removed_total)


def remap_words(words: list, plan: SilencePlan, clip_start: float) -> list:
    """Devuelve copias de las palabras con tiempos remapeados según el plan."""
    from worker.pipeline import Word  # import local para evitar ciclo en tests

    out = []
    for w in words:
        out.append(Word(w.text, plan.remap(w.start - clip_start),
                        plan.remap(w.end - clip_start)))
    return out
