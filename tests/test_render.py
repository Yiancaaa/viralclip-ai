"""Render vertical: genera un vídeo de prueba con ffmpeg y verifica con
ffprobe que el clip sale 1080x1920, h264 y aac."""
import json
import subprocess

import pytest

from worker.pipeline import render_clip


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


def test_render_vertical_1080x1920(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "clip.mp4")
    _make_source(src)
    subs = [{"start": 0.0, "end": 1.5, "text": "SI HAY UNA CASA"},
            {"start": 1.5, "end": 3.0, "text": "EN TU CALLE"}]
    render_clip(src, 1.0, 4.0, subs, out, keyword="CASA")
    info = _probe(out)
    video = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"][0]
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["codec_name"] == "h264"
    assert audio["codec_name"] == "aac"


def test_render_duration_matches(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "clip.mp4")
    _make_source(src)
    render_clip(src, 0.5, 2.5, [{"start": 0.0, "end": 2.0, "text": "HOLA"}], out)
    dur = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", out], text=True).strip())
    assert abs(dur - 2.0) < 0.3
