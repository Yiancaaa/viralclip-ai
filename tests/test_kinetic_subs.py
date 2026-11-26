"""Motor de subtítulos cinéticos: ASS por palabra, clasificación de
efectos, recorte de silencios y render dinámico verificado con ffprobe."""
import json
import subprocess

import pytest

from worker.kinetic_subs import (
    build_kinetic_ass,
    chunk_words,
    classify_block,
    silence_plan,
    zoom_crop_expression,
    zoom_windows,
)
from worker.pipeline import Word, render_clip


def _words():
    return [
        Word("Si", 0.5, 0.8), Word("hay", 0.8, 1.0), Word("una", 1.0, 1.2),
        Word("casa", 1.2, 1.6), Word("en", 1.6, 1.8), Word("tu", 1.8, 2.0),
        Word("calle", 2.0, 2.5),
        Word("de", 2.7, 2.9), Word("repente", 2.9, 3.4), Word("qué", 3.4, 3.7),
        Word("pasó?", 3.7, 4.2),
        Word("Un", 4.4, 4.6), Word("clic!", 4.6, 5.2),
    ]


def _make_source(path: str, duration: int = 6) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=24:duration={duration}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", path], check=True)


def _probe(path: str) -> dict:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_streams", "-show_entries",
         "stream=codec_type,codec_name,width,height", "-of", "json", path], text=True)
    return json.loads(out)


def _duration(path: str) -> float:
    return float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path], text=True).strip())


# ------------------------------------------------------------------ ASS
def test_kinetic_ass_contains_word_timestamps_and_transforms():
    ass, effects = build_kinetic_ass(_words(), clip_start=0.0, keyword="casa")
    # Timestamps de palabra reales (no inventados): cada palabra aparece
    # en un evento que empieza en su start.
    assert "0:00:01.20" in ass  # "casa" empieza a 1.2 s
    assert "0:00:04.60" in ass  # "clic!" empieza a 4.6 s
    # Transformaciones \t de entrada/pop en la palabra actual
    assert "\\t(0,80,\\fscx112\\fscy112)" in ass
    assert "\\t(80,180,\\fscx100\\fscy100)" in ass
    # Palabra clave resaltada en amarillo VIRALCLIP, más grande
    assert "&H0000D4FF&\\fs84" in ass
    # Impacto ("clic!") en rosa, aún más grande
    assert "&H006040FF&\\fs96" in ass
    # Bloques de <=6 palabras: ningún evento tiene más de 6 palabras
    assert all(len(e["text"].split()) <= 6 for e in effects)


def test_kinetic_ass_uses_real_words_only():
    ass, _ = build_kinetic_ass(_words(), clip_start=0.0)
    for w in ("casa", "repente", "clic!"):
        assert w in ass
    assert "inventada" not in ass


def test_chunk_words_max_six_and_pause_split():
    words = [Word(f"w{i}", float(i), float(i) + 0.4) for i in range(8)]
    blocks = chunk_words(words)
    assert all(len(b) <= 6 for b in blocks)
    assert sum(len(b) for b in blocks) == 8
    # Pausa >=0,5 s corta el bloque aunque no llegue a 6 palabras
    words2 = [Word("a", 0.0, 0.4), Word("b", 1.0, 1.4), Word("c", 1.5, 1.9)]
    assert len(chunk_words(words2)) == 2


# ------------------------------------------------------------------ clasificación
def test_classify_impact_surprise_highlight_tension_normal():
    assert classify_block([Word("Un", 0, 0.2), Word("clic!", 0.2, 0.6)]).kind == "impact"
    assert classify_block([Word("de", 0, 0.2), Word("repente", 0.2, 0.6)]).kind == "surprise"
    assert classify_block([Word("quién", 0, 0.3), Word("la", 0.3, 0.5),
                           Word("tomó?", 0.5, 0.9)]).kind == "surprise"
    assert classify_block([Word("Regla", 0, 0.4), Word("1", 0.4, 0.6)]).kind == "highlight"
    assert classify_block([Word("siete", 0, 0.4), Word("ventanas", 0.4, 0.9)]).kind == "highlight"
    assert classify_block([Word("las", 0, 0.2), Word("paredes", 0.2, 0.6),
                           Word("respiran", 0.6, 1.0)]).kind == "tension"
    assert classify_block([Word("cada", 0, 0.3), Word("noche", 0.3, 0.6)]).kind == "tension"
    assert classify_block([Word("una", 0, 0.3), Word("mesa", 0.3, 0.7)]).kind == "normal"


def test_classify_uses_keyword():
    eff = classify_block([Word("la", 0, 0.2), Word("casa", 0.2, 0.6)], keyword="casa")
    assert eff.kind == "highlight"
    assert eff.word_idx == [1]


def test_zoom_windows_only_for_surprise():
    _, effects = build_kinetic_ass(_words(), clip_start=0.0)
    windows = zoom_windows(effects)
    assert len(windows) == 1
    s, e = windows[0]
    assert s <= 2.0 and e >= 4.2  # cubre el bloque "de repente... pasó?"
    expr = zoom_crop_expression(windows)
    # if(between(...)) perezoso: no propaga NaN al configurar el crop
    assert "if(between(t" in expr
    assert "abs(" not in expr


# ------------------------------------------------------------------ silencios
def test_silence_plan_cuts_long_gap_only():
    words = [Word("Hola", 0.0, 0.5), Word("mundo", 3.0, 3.5)]
    plan = silence_plan(words, 0.0, 4.0)
    assert plan.total_removed == pytest.approx(2.1)  # gap 2,5 s - 0,4 s conservados
    assert plan.remap(3.0) == pytest.approx(0.9)    # "mundo" se adelanta
    assert plan.remap(0.4) == pytest.approx(0.4)    # antes del gap, sin cambio


def test_silence_plan_noop_without_long_gaps():
    words = [Word("a", 0.0, 0.4), Word("b", 0.9, 1.3), Word("c", 1.8, 2.2)]
    plan = silence_plan(words, 0.0, 2.5)
    assert plan.total_removed == 0.0
    assert len(plan.keep) == 1


# ------------------------------------------------------------------ render
def test_render_dynamic_vertical_ffprobe(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "clip.mp4")
    _make_source(src)
    render_clip(src, 0.0, 6.0, [], out, keyword="casa",
                style="dynamic", words=_words())
    info = _probe(out)
    video = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"][0]
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["codec_name"] == "h264"
    assert audio["codec_name"] == "aac"
    assert abs(_duration(out) - 6.0) < 0.3


def test_render_dynamic_trims_long_silence(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "clip.mp4")
    _make_source(src)
    words = [Word("Hola", 0.0, 0.5), Word("mundo", 3.0, 3.5)]
    render_clip(src, 0.0, 4.0, [], out, style="dynamic", words=words)
    # 4,0 s - 2,1 s de silencio recortado = ~1,9 s
    assert abs(_duration(out) - 1.9) < 0.3


def test_render_dynamic_requires_words(tmp_path):
    src = str(tmp_path / "src.mp4")
    _make_source(src)
    with pytest.raises(ValueError, match="necesita `words`"):
        render_clip(src, 0.0, 2.0, [], str(tmp_path / "c.mp4"), style="dynamic")


def test_render_dynamic_raised_position(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "clip.mp4")
    _make_source(src)
    render_clip(src, 0.0, 6.0, [], out, style="dynamic", words=_words(),
                subtitle_position="raised")
    ass = (tmp_path / "clip.ass").read_text(encoding="utf-8")
    assert "60,60,720,1" in ass  # MarginV 720 = subtítulos por encima del tercio inferior
    assert abs(_duration(out) - 6.0) < 0.3
