"""Render dinámico + layout con facecam unificado (worker/pipeline.py) y
conexión de la detección a process_job (worker/jobs.py).

(a) Render dinámico + facecam_bottom con vídeo sintético y con el vídeo
real de La Casa (contenido propio), facecam simulada por bbox explícita
(no se inventa detección: el dict se pasa a mano).
(b) La combinación dynamic + layout ya NO lanza ValueError.
(c) process_job con vídeo de gameplay usa el layout decidido (visión con
mocks: sample_frames/detect_facecam simulados; el render es real).
(d) Los subtítulos no tapan la facecam: con facecam abajo van en posición
"raised" (MarginV 720 en el ASS cinético), por encima del pane de
facecam (y 1440-1920 en el lienzo 1080x1920).
"""
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from viralclip.models import Job, Video
from viralclip.storage import LocalStorage
from worker.jobs import process_job
from worker.pipeline import TranscriptSegment, Word, render_clip

LA_CASA = Path("/home/hatch/workspace/proyectos/la-casa-que-solo-existe-de-noche/"
               "La_Casa_Que_Solo_Existe_De_Noche_FINAL.mp4")

# Pane de facecam en el lienzo vertical 1080x1920 (ver worker/vision.py).
FACECAM_PANE_H = 480
FACECAM_PANE_TOP_Y = 1920 - FACECAM_PANE_H  # 1440


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


def _assert_vertical(out: str) -> None:
    info = _probe(out)
    video = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"][0]
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["codec_name"] == "h264"
    assert audio["codec_name"] == "aac"


def _words():
    return [
        Word("Si", 0.5, 0.8), Word("hay", 0.8, 1.0), Word("una", 1.0, 1.2),
        Word("casa", 1.2, 1.6), Word("en", 1.6, 1.8), Word("tu", 1.8, 2.0),
        Word("calle", 2.0, 2.5),
        Word("de", 2.7, 2.9), Word("repente", 2.9, 3.4), Word("qué", 3.4, 3.7),
        Word("pasó?", 3.7, 4.2),
        Word("Un", 4.4, 4.6), Word("clic!", 4.6, 5.2),
    ]


def _facecam(faces=None):
    return {
        "present": True,
        "bbox": (0.004, 0.368, 0.187, 0.333),
        "position": "bottom-left",
        "confidence": 0.91,
        "stable": True,
        "face_bbox": (0.07, 0.535, 0.055, 0.098),
        "coverage": 1.0,
        "n_frames": 91,
        "faces": faces if faces is not None else [
            {"bbox": (0.07, 0.535, 0.055, 0.098), "coverage": 1.0,
             "center": (0.096, 0.584), "std": (0.003, 0.006)}],
        "median_face_rel_area": 0.005,
        "large_face": False,
    }


def _ass_margin_v(ass_text: str) -> int:
    """MarginV del estilo en el ASS generado (...,60,60,<MarginV>,1)."""
    for line in ass_text.splitlines():
        if line.startswith("Style:"):
            return int(line.rsplit(",", 2)[-2])
    raise AssertionError("sin Style en el ASS")


# ------------------------------------------------------------------ (b) no ValueError
def test_dynamic_facecam_bottom_no_longer_raises(tmp_path):
    """(b) dynamic + facecam_bottom ya no lanza ValueError: renderiza
    1080x1920 h264+aac y el ASS cinético va en posición raised."""
    src, out = str(tmp_path / "src.mp4"), str(tmp_path / "clip.mp4")
    _make_source(src)
    render_clip(src, 0.0, 6.0, [], out, keyword="casa", style="dynamic",
                words=_words(), layout="facecam_bottom", facecam=_facecam())
    _assert_vertical(out)
    ass = (tmp_path / "clip.ass").read_text(encoding="utf-8")
    assert "60,60,720,1" in ass  # raised


def test_dynamic_other_layouts_no_longer_raise(tmp_path):
    """(b) dynamic + facecam_top / talking_head / multi_window tampoco
    lanzan ValueError."""
    src = str(tmp_path / "src.mp4")
    _make_source(src)
    two_faces = [
        {"bbox": (0.1, 0.3, 0.06, 0.1), "coverage": 0.9,
         "center": (0.13, 0.35), "std": (0.002, 0.002)},
        {"bbox": (0.7, 0.3, 0.06, 0.1), "coverage": 0.85,
         "center": (0.73, 0.35), "std": (0.002, 0.002)},
    ]
    for layout, fc in [("facecam_top", _facecam()),
                       ("talking_head", _facecam()),
                       ("multi_window", _facecam(faces=two_faces))]:
        out = str(tmp_path / f"{layout}.mp4")
        render_clip(src, 0.0, 6.0, [], out, style="dynamic", words=_words(),
                    layout=layout, facecam=fc)
        _assert_vertical(out)


def test_dynamic_unknown_layout_still_raises(tmp_path):
    src = str(tmp_path / "src.mp4")
    _make_source(src)
    with pytest.raises(ValueError, match="layout desconocido"):
        render_clip(src, 0.0, 2.0, [], str(tmp_path / "c.mp4"),
                    style="dynamic", words=_words(), layout="inexistente")


# ------------------------------------------------------------------ (a) La Casa real
def test_dynamic_facecam_bottom_with_la_casa_real_video(tmp_path):
    """(a) Vídeo real de La Casa (contenido propio, 1080x1920) + facecam
    simulada por bbox explícita: clip dinámico con facecam abajo,
    1080x1920 h264+aac."""
    if not LA_CASA.exists():
        pytest.skip("vídeo de La Casa no presente")
    out = str(tmp_path / "lacasa_dyn_fb.mp4")
    render_clip(str(LA_CASA), 0.0, 6.0, [], out, keyword="casa",
                style="dynamic", words=_words(),
                layout="facecam_bottom", facecam=_facecam())
    _assert_vertical(out)


def test_dynamic_facecam_bottom_trims_silence_and_keeps_layout(tmp_path):
    """(a/b) Con recorte de silencios, dynamic + facecam_bottom también
    compone (trim sobre el origen + composición tras el concat)."""
    src, out = str(tmp_path / "src.mp4"), str(tmp_path / "clip.mp4")
    _make_source(src)
    words = [Word("Hola", 0.0, 0.5), Word("mundo", 3.0, 3.5)]
    render_clip(src, 0.0, 4.0, [], out, style="dynamic", words=words,
                layout="facecam_bottom", facecam=_facecam())
    _assert_vertical(out)
    dur = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", out], text=True).strip())
    assert abs(dur - 1.9) < 0.3  # 4,0 s - 2,1 s de silencio recortado


# ------------------------------------------------------------------ fallbacks documentados
def test_dynamic_facecam_bottom_without_facecam_falls_back(tmp_path):
    """Fallback documentado: dynamic + facecam_bottom sin facecam válida
    cae a gameplay_only dinámico (sin error, subtítulos abajo)."""
    src, out = str(tmp_path / "src.mp4"), str(tmp_path / "clip.mp4")
    _make_source(src)
    render_clip(src, 0.0, 6.0, [], out, style="dynamic", words=_words(),
                layout="facecam_bottom", facecam=None)
    _assert_vertical(out)
    ass = (tmp_path / "clip.ass").read_text(encoding="utf-8")
    assert "60,60,200,1" in ass  # bottom, no hay pane que evitar


def test_dynamic_multi_window_single_face_falls_back_to_facecam_top(tmp_path):
    """Fallback documentado: multi_window con un solo rostro cae a
    facecam_top dinámico."""
    src, out = str(tmp_path / "src.mp4"), str(tmp_path / "clip.mp4")
    _make_source(src)
    render_clip(src, 0.0, 6.0, [], out, style="dynamic", words=_words(),
                layout="multi_window", facecam=_facecam())
    _assert_vertical(out)


# ------------------------------------------------------------------ (d) subtítulos no tapan la facecam
def test_subtitles_do_not_cover_facecam_bottom(tmp_path):
    """(d) Con facecam abajo, los subtítulos cinéticos van raised
    (MarginV 720): su borde inferior queda en y=1920-720=1200, por
    encima del pane de facecam, que empieza en y=1440."""
    src, out = str(tmp_path / "src.mp4"), str(tmp_path / "clip.mp4")
    _make_source(src)
    render_clip(src, 0.0, 6.0, [], out, style="dynamic", words=_words(),
                layout="facecam_bottom", facecam=_facecam())
    ass = (tmp_path / "clip.ass").read_text(encoding="utf-8")
    margin_v = _ass_margin_v(ass)
    assert margin_v == 720
    subtitles_bottom_y = 1920 - margin_v
    assert subtitles_bottom_y < FACECAM_PANE_TOP_Y, (
        f"subtítulos hasta y={subtitles_bottom_y}, pane desde y={FACECAM_PANE_TOP_Y}")


def test_subtitles_stay_bottom_with_facecam_top(tmp_path):
    """(d) Con facecam arriba, los subtítulos quedan abajo (MarginV 200),
    sobre el gameplay: la facecam (y 0-480) no se tapa."""
    src, out = str(tmp_path / "src.mp4"), str(tmp_path / "clip.mp4")
    _make_source(src)
    render_clip(src, 0.0, 6.0, [], out, style="dynamic", words=_words(),
                layout="facecam_top", facecam=_facecam())
    ass = (tmp_path / "clip.ass").read_text(encoding="utf-8")
    assert _ass_margin_v(ass) == 200


# ------------------------------------------------------------------ (c) process_job
def _mock_segments():
    """18 palabras neutras de 0,5 a ~15,4 s: un solo momento detectable."""
    words = []
    for i in range(18):
        s = 0.5 + i * 0.85
        words.append(Word(f"palabra{i}", s, s + 0.5))
    text = " ".join(w.text for w in words)
    return [TranscriptSegment(text, 0.5, words[-1].end, words)]


def _setup_video_job(db, tmp_path, category, duration=20):
    src = str(tmp_path / "src.mp4")
    _make_source(src, duration=duration)
    storage = LocalStorage(tmp_path / "store")
    storage.save("src.mp4", src)
    video = Video(title="gameplay prueba", platform="upload",
                  file_path="src.mp4", category=category)
    db.add(video)
    db.commit()
    job = Job(video_id=video.id, kind="process", status="queued")
    db.add(job)
    db.commit()
    return storage, video, job


def test_process_job_gameplay_uses_decided_layout(db, tmp_path, monkeypatch):
    """(c) process_job con categoría gameplay_streamer: la visión (con
    mocks) decide facecam_bottom, el clip se renderiza dinámico con ese
    layout y el resultado lo registra."""
    storage, video, job = _setup_video_job(db, tmp_path, "gameplay_streamer")
    monkeypatch.setattr("worker.jobs.pipeline.transcribe",
                        lambda audio, **kw: _mock_segments())
    monkeypatch.setattr("worker.vision.sample_frames",
                        lambda *a, **k: [(0.0, np.zeros((100, 100, 3), dtype=np.uint8))])
    monkeypatch.setattr("worker.vision.detect_facecam",
                        lambda frames, **k: _facecam())
    result = process_job(db, job.id, storage, workdir=str(tmp_path / "work"))
    assert result["status"] == "done"
    assert result["facecam_present"] is True
    assert result["vision_available"] is True
    assert len(result["clips"]) == 1
    assert result["clips"][0]["layout"] == "facecam_bottom"
    assert result["clips"][0]["style"] == "dynamic"
    _assert_vertical(str(tmp_path / "store" / result["clips"][0]["clip"]))


def test_process_job_without_vision_falls_back_to_gameplay_only(db, tmp_path, monkeypatch):
    """(c) Sin OpenCV/detección (sample_frames falla), el job NO se rompe:
    cae a gameplay_only dinámico."""
    storage, video, job = _setup_video_job(db, tmp_path, "gameplay_streamer")
    monkeypatch.setattr("worker.jobs.pipeline.transcribe",
                        lambda audio, **kw: _mock_segments())

    def _boom(*a, **k):
        raise ImportError("cv2 no disponible (simulado)")

    monkeypatch.setattr("worker.vision.sample_frames", _boom)
    result = process_job(db, job.id, storage, workdir=str(tmp_path / "work"))
    assert result["status"] == "done"
    assert result["vision_available"] is False
    assert result["facecam_present"] is False
    assert result["clips"][0]["layout"] == "gameplay_only"
    assert result["clips"][0]["style"] == "dynamic"
    _assert_vertical(str(tmp_path / "store" / result["clips"][0]["clip"]))


def test_process_job_non_gameplay_does_not_sample_frames(db, tmp_path, monkeypatch):
    """(c) Sin categoría gameplay no se intenta detectar facecam: el
    render es dinámico gameplay_only (con focus_x si lo hubiera)."""
    storage, video, job = _setup_video_job(db, tmp_path, "conversacion_reaccion")
    monkeypatch.setattr("worker.jobs.pipeline.transcribe",
                        lambda audio, **kw: _mock_segments())

    def _must_not_run(*a, **k):
        raise AssertionError("no se debe muestrear fuera de gameplay")

    monkeypatch.setattr("worker.vision.sample_frames", _must_not_run)
    result = process_job(db, job.id, storage, workdir=str(tmp_path / "work"))
    assert result["status"] == "done"
    assert result["vision_available"] is False
    assert result["clips"][0]["layout"] == "gameplay_only"
    assert result["clips"][0]["style"] == "dynamic"
