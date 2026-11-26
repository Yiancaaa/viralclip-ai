"""Transcripción real con faster-whisper (modelo tiny, CPU) sobre los
primeros 10 s de la narración de Nora. Ground truth: ground_truth.txt."""
import subprocess

import pytest

from worker.pipeline import transcribe

NARRACION = "/home/hatch/workspace/proyectos/la-casa-que-solo-existe-de-noche/narracion_full.mp3"
GT = "/home/hatch/workspace/viralclip-ai/docs/ground_truth.txt"


def test_transcribe_nora_first_10s(tmp_path):
    wav = str(tmp_path / "nora10.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", NARRACION, "-t", "10",
                    "-ac", "1", "-ar", "16000", wav], check=True)
    segments = transcribe(wav, model="tiny", device="cpu", compute_type="int8")
    text = " ".join(s.text for s in segments)
    assert len(text.strip()) > 10, f"transcripción vacía: {segments!r}"
    # La narración empieza: "Si hay una casa en tu calle..."
    assert "casa" in text.lower()
    # Timestamps de palabra presentes (necesarios para no cortar palabras)
    assert any(s.words for s in segments)
