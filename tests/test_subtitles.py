"""subtitles_from_segments: agrupación de palabras en líneas.

Regla portada de e2e/work/make_ass.py: una línea se corta en una pausa
> 0,8 s entre palabras (misma frontera que detect_moments), además del
tope de 6 palabras por línea.
"""
from worker.pipeline import TranscriptSegment, Word, subtitles_from_segments


def _seg(words):
    return TranscriptSegment(" ".join(w.text for w in words), words[0].start,
                             words[-1].end, words)


def test_split_on_long_pause():
    # 4 palabras con una pausa de 1,2 s entre la 2ª y la 3ª -> 2 líneas
    words = [Word("si", 0.0, 0.3), Word("hay", 0.4, 0.7),
             Word("una", 1.9, 2.2), Word("casa", 2.3, 2.6)]
    subs = subtitles_from_segments([_seg(words)], 0.0, 3.0)
    assert [s["text"] for s in subs] == ["si hay", "una casa"]
    assert subs[0]["end"] == 0.7 and subs[1]["start"] == 1.9


def test_no_split_on_short_pause():
    # Pausa de 0,5 s: misma línea (el umbral es > 0,8 s)
    words = [Word("si", 0.0, 0.3), Word("hay", 0.8, 1.1),
             Word("una", 1.2, 1.5), Word("casa", 1.6, 1.9)]
    subs = subtitles_from_segments([_seg(words)], 0.0, 3.0)
    assert [s["text"] for s in subs] == ["si hay una casa"]


def test_six_word_cap_still_applies():
    words = [Word(f"w{i}", i * 0.4, i * 0.4 + 0.3) for i in range(8)]
    subs = subtitles_from_segments([_seg(words)], 0.0, 10.0)
    assert [s["text"] for s in subs] == ["w0 w1 w2 w3 w4 w5", "w6 w7"]


def test_segment_fallback_without_words():
    seg = TranscriptSegment("noche oscura", 1.0, 3.0, [])
    subs = subtitles_from_segments([seg], 0.0, 5.0)
    assert subs == [{"start": 1.0, "end": 3.0, "text": "noche oscura"}]
