"""Detección de facecam/gameplay (worker/vision.py) y composición vertical
en render_clip: geometría sintética + detección real sobre 3 fotogramas
del segmento de prueba (fixtures, contenido de un tercero solo para
verificación interna, no redistribuir).

Los umbrales del test real (confianza > 0,5, bottom-left) son los medidos
en la prueba real: 91 fotogramas a 1 fps dieron confianza 0,91 y estos
3 fotogramas dan 0,786. Si la detección real fallara, el test debe
fallar (no se baja el umbral sin evidencia).
"""
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from worker.pipeline import render_clip
from worker.vision import (
    _cluster_faces,
    _face_to_facecam_box,
    _position_of,
    decide_layout,
    detect_facecam,
    detect_gameplay_area,
    detect_static_regions,
    smooth_bbox_series,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _facecam(present=True, position="bottom-left", face_area=0.005,
             faces=None, large_face=False):
    return {
        "present": present,
        "bbox": (0.004, 0.368, 0.187, 0.333),
        "position": position,
        "confidence": 0.91,
        "stable": True,
        "face_bbox": (0.07, 0.535, 0.055, 0.098),
        "coverage": 1.0,
        "n_frames": 91,
        "faces": faces if faces is not None else [
            {"bbox": (0.07, 0.535, 0.055, 0.098), "coverage": 1.0,
             "center": (0.096, 0.584), "std": (0.003, 0.006)}],
        "median_face_rel_area": face_area,
        "large_face": large_face,
    }


# ------------------------------------------------------------------ geometría
def test_cluster_faces_stable_vs_spurious():
    # Rostro estable 5 fotogramas en el mismo sitio + 5 detecciones
    # sueltas (falsos positivos del gameplay, cada una en un sitio).
    dets = [(0.07, 0.53, 0.055, 0.098)] * 5
    dets += [(0.5, 0.2, 0.05, 0.09), (0.3, 0.7, 0.05, 0.09),
             (0.8, 0.1, 0.05, 0.09), (0.2, 0.4, 0.06, 0.1),
             (0.6, 0.6, 0.05, 0.09)]
    clusters = _cluster_faces(dets)
    assert clusters[0]["count"] == 5
    assert all(c["count"] == 1 for c in clusters[1:])
    cx, cy = clusters[0]["center"]
    assert abs(cx - 0.097) < 0.01 and abs(cy - 0.58) < 0.01


def test_smooth_bbox_series_moving_average():
    boxes = [(0.0, 0.0, 1.0, 1.0), (0.2, 0.0, 1.0, 1.0), (0.4, 0.0, 1.0, 1.0)]
    out = smooth_bbox_series(boxes, window=2)
    assert out[0] == (0.0, 0.0, 1.0, 1.0)
    assert out[1][0] == pytest.approx(0.1)
    assert out[2][0] == pytest.approx(0.3)


def test_face_to_facecam_box_contains_face_and_matches_real():
    # Rostro medido en el segmento real -> caja de facecam medida a mano
    # (0.003-0.19, 0.36-0.70). La estimación debe contener el rostro y
    # acercarse a la caja real (IoU > 0,5).
    face = (0.07, 0.535, 0.055, 0.098)
    x, y, w, h = _face_to_facecam_box(face)
    assert x <= 0.07 and y <= 0.535 and x + w >= 0.125 and y + h >= 0.633
    assert abs(x - 0.003) < 0.02 and abs(y - 0.36) < 0.02
    assert abs(w - 0.187) < 0.02 and abs(h - 0.333) < 0.02
    # Caja ~cuadrada en píxeles (1920x1080): w_px ~= h_px.
    assert abs(w * 1920 - h * 1080) < 40


def test_position_of():
    assert _position_of((0.1, 0.8)) == "bottom-left"
    assert _position_of((0.9, 0.1)) == "top-right"
    assert _position_of((0.5, 0.5)) == "center"


def test_detect_gameplay_area_excludes_facecam():
    frame = np.zeros((540, 960, 3), dtype=np.uint8)
    gp = detect_gameplay_area(frame, (0.004, 0.368, 0.187, 0.333))
    assert gp["bbox"][0] == pytest.approx(0.191, abs=0.01)
    assert gp["bbox"][2] * gp["bbox"][3] == pytest.approx(0.809, abs=0.01)
    assert gp["facecam_bbox"] == (0.004, 0.368, 0.187, 0.333)
    full = detect_gameplay_area(frame, None)
    assert full["bbox"] == (0.0, 0.0, 1.0, 1.0)


def test_detect_static_regions_finds_static_rect():
    rng = np.random.default_rng(7)
    frames = []
    for _ in range(6):
        img = rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)
        # Rectángulo estático (HUD) + cuadrado que se mueve (gameplay).
        cv2.rectangle(img, (50, 50), (200, 150), (10, 10, 10), -1)
        frames.append(img)
    regions = detect_static_regions(frames)
    assert regions, "no se detectó ninguna región estática"
    # La caja detectada debe solaparse fuertemente con el rectángulo
    # estático (IoU > 0,5): el ruido aleatorio del fondo puede inflar
    # algo la caja por azar, pero el rectángulo debe dominar la región.
    rx, ry, rw, rh = regions[0]["bbox"]
    ex, ey, ew, eh = 50 / 320, 50 / 240, 150 / 320, 100 / 240
    ix = max(0.0, min(rx + rw, ex + ew) - max(rx, ex))
    iy = max(0.0, min(ry + rh, ey + eh) - max(ry, ey))
    inter = ix * iy
    union = rw * rh + ew * eh - inter
    assert inter / union > 0.5, f"IoU bajo: {regions[0]}"


# ------------------------------------------------------------------ layout
def test_decide_layout_gameplay_only_without_facecam():
    assert decide_layout(_facecam(present=False, faces=[])) == "gameplay_only"


def test_decide_layout_facecam_bottom_for_action():
    # Momento de acción del juego (sin reacción): gameplay arriba.
    assert decide_layout(_facecam(), None, {}) == "facecam_bottom"


def test_decide_layout_facecam_top_for_reaction():
    assert decide_layout(_facecam(), None, {"energy": 0.9}) == "facecam_top"
    assert decide_layout(_facecam(), None, {"reaction": True}) == "facecam_top"


def test_decide_layout_talking_head_for_large_face():
    fc = _facecam(present=False, faces=[], large_face=True)
    assert decide_layout(fc) == "talking_head"


def test_decide_layout_multi_window_for_two_stable_faces():
    faces = [
        {"bbox": (0.1, 0.3, 0.06, 0.1), "coverage": 0.9,
         "center": (0.13, 0.35), "std": (0.002, 0.002)},
        {"bbox": (0.7, 0.3, 0.06, 0.1), "coverage": 0.85,
         "center": (0.73, 0.35), "std": (0.002, 0.002)},
    ]
    assert decide_layout(_facecam(faces=faces)) == "multi_window"


# ------------------------------------------------------------------ detección
def test_detect_facecam_no_faces():
    black = [np.zeros((540, 960, 3), dtype=np.uint8) for _ in range(5)]
    r = detect_facecam(black)
    assert r["present"] is False
    assert r["confidence"] == 0.0
    assert r["coverage"] == 0.0


def test_detect_facecam_real_frames():
    """3 fotogramas reales del segmento (t=28, 38, 52 s). Medido antes de
    escribir el test: confianza 0,786, bottom-left, cobertura 1,0."""
    paths = [FIXTURES / f"frame_t{t}.jpg" for t in ("028", "038", "052")]
    if not all(p.exists() for p in paths):
        pytest.skip("fixtures de fotogramas reales no presentes")
    frames = [cv2.imread(str(p)) for p in paths]
    assert all(f is not None for f in frames)
    r = detect_facecam(frames)
    assert r["present"] is True
    assert r["confidence"] > 0.5, f"confianza real: {r}"
    assert r["position"] == "bottom-left"
    assert r["coverage"] >= 0.9
    assert r["stable"] is True


# ------------------------------------------------------------------ render
def _make_source(path: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
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


def test_render_facecam_bottom(tmp_path):
    src, out = str(tmp_path / "src.mp4"), str(tmp_path / "clip.mp4")
    _make_source(src)
    render_clip(src, 1.0, 4.0, [{"start": 0.0, "end": 3.0, "text": "HOLA"}],
                out, layout="facecam_bottom", facecam=_facecam())
    _assert_vertical(out)


def test_render_facecam_top_and_talking_head(tmp_path):
    src = str(tmp_path / "src.mp4")
    _make_source(src)
    out_top = str(tmp_path / "top.mp4")
    render_clip(src, 1.0, 3.0, [{"start": 0.0, "end": 2.0, "text": "HOLA"}],
                out_top, layout="facecam_top", facecam=_facecam())
    _assert_vertical(out_top)
    out_th = str(tmp_path / "th.mp4")
    render_clip(src, 1.0, 3.0, [{"start": 0.0, "end": 2.0, "text": "HOLA"}],
                out_th, layout="talking_head", facecam=_facecam())
    _assert_vertical(out_th)


def test_render_layout_falls_back_without_facecam(tmp_path):
    src, out = str(tmp_path / "src.mp4"), str(tmp_path / "clip.mp4")
    _make_source(src)
    render_clip(src, 1.0, 3.0, [{"start": 0.0, "end": 2.0, "text": "HOLA"}],
                out, layout="facecam_top", facecam=None)
    _assert_vertical(out)


def test_render_unknown_layout_raises(tmp_path):
    src, out = str(tmp_path / "src.mp4"), str(tmp_path / "clip.mp4")
    _make_source(src)
    with pytest.raises(ValueError):
        render_clip(src, 1.0, 2.0, [], out, layout="inexistente")
