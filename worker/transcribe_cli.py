"""Transcripción con faster-whisper en un proceso aparte.

Uso: ``python -m worker.transcribe_cli audio.wav tiny cpu int8 es 1 0``
Imprime los segmentos en JSON por stdout. Los dos últimos argumentos
son opcionales: hilos de CPU (dieta: 1) y VAD 1/0 (dieta: 0, sin VAD no
se carga onnxruntime).

El proceso web nunca importa faster-whisper: la memoria del modelo y
de sus librerías C (CTranslate2, tokenizers, onnxruntime del VAD) se
libera por completo al salir este proceso. En el plan Free de Render
(512 MB) transcribir dentro del proceso web tumbaba el servidor por
falta de memoria (pico medido 849 MB con tiny, 1043 MB con small,
2026-11-26); con el subproceso, el pico del proceso web baja a la
transcripción por separado y el render posterior.
"""
import json
import sys


def main(argv: list[str]) -> int:
    audio, model, device, compute_type, language = argv[1:6]
    cpu_threads = int(argv[6]) if len(argv) > 6 else 4
    vad_filter = (argv[7] != "0") if len(argv) > 7 else True
    from worker.pipeline import segments_to_json, transcribe

    segments = transcribe(audio, model=model, device=device,
                          compute_type=compute_type, language=language,
                          cpu_threads=cpu_threads, vad_filter=vad_filter)
    json.dump(segments_to_json(segments), sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
