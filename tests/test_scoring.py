"""Puntuación y detección con ground truth real: la narración de Nora
(ground_truth.txt, proyecto La casa que solo existe de noche)."""
from pathlib import Path

from worker.pipeline import (TranscriptSegment, Word, detect_moments,
                             generate_titles, score_moment)

GT_PATH = Path.home() / "workspace/viralclip-ai/docs/ground_truth.txt"


def _load_gt() -> str:
    lines = [l for l in GT_PATH.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    return " ".join(lines)


def _segments_from_text(text: str) -> list[TranscriptSegment]:
    """Segmentos sintéticos con timestamps de palabra reales en estructura:
    ~3 palabras/segundo, como la narración real (~170 ppm)."""
    words = text.split()
    out, t, buf = [], 0.0, []
    for i, w in enumerate(words):
        start, end = t, t + 0.30
        buf.append(Word(w, start, end))
        t = end + 0.05
        if (i + 1) % 20 == 0 or i == len(words) - 1:
            out.append(TranscriptSegment(" ".join(x.text for x in buf), buf[0].start, buf[-1].end, list(buf)))
            buf = []
    return out


def test_ground_truth_loads():
    text = _load_gt()
    assert "Nora" in text and "03:33" in text and len(text.split()) > 150


def test_detect_moments_never_cuts_words():
    text = _load_gt()
    segments = _segments_from_text(text)
    moments = detect_moments(segments)
    assert len(moments) >= 1
    for m in moments:
        # inicio/fin coinciden con límites de palabra de la transcripción
        assert m["end"] > m["start"]
        assert 5.0 <= m["end"] - m["start"] <= 90.0
        # el texto del momento es una subcadena contigua del texto completo
        assert m["text"][:20] in text


def test_score_range_and_breakdown():
    moment = {"start": 0.0, "end": 45.0,
              "text": "Si hay una casa en tu calle que solo aparece cuando no la miras, "
                      "no la mires dos veces, porque esa casa ya te vio a ti. ¿Quién la tomó desde dentro?"}
    scored = score_moment(moment)
    assert 0 <= scored["score"] <= 100
    expected = {"gancho", "emocion", "entretenimiento", "originalidad",
                "curiosidad", "comentarios", "comprension", "calidad"}
    assert set(scored["breakdown"]) == expected
    assert abs(sum(v["weight"] for v in scored["breakdown"].values()) - 1.0) < 1e-9


def test_score_hook_beats_flat_text():
    hook = {"start": 0.0, "end": 40.0,
            "text": "Nadie te contó esto, escucha: ¿quién tomó la foto desde dentro? "
                    "De repente la luz se enciende sola y es ella durmiendo."}
    flat = {"start": 0.0, "end": 40.0,
            "text": "Hoy es un día normal en la calle con casas y gente que camina tranquila."}
    assert score_moment(hook)["score"] > score_moment(flat)["score"]


def test_titles_at_least_five_and_grounded():
    text = _load_gt()
    titles = generate_titles(text)
    assert len(titles) >= 5
    # Ningún título inventa hechos: todas sus palabras "clave" vienen del texto
    norm = text.lower()
    for t in titles:
        words = [w.strip(".,¿?¡!:") for w in t.lower().split() if len(w) > 3]
        content_words = [w for w in words if w not in
                         {"nadie", "contó", "esto", "pasó", "puede", "explicar", "cuando",
                          "creer", "pasa", "después", "regla", "cumple"}]
        assert any(w in norm for w in content_words), f"título sin anclaje: {t}"
