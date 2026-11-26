"""Clasificación del tipo de directo: estructura, prioridad manual,
pesos por categoría, baja confianza y endpoints."""
import json
import subprocess

from fastapi.testclient import TestClient

from api.main import app
from viralclip.models import Video
from worker.classify import (BUTTONS, CATEGORIES, classify_from_signals,
                             effective_category, layout_for_category)
from worker.pipeline import CATEGORY_WEIGHTS, render_clip, score_moment

client = TestClient(app)


def _gameplay_signals():
    video = {"opencv_available": True, "frames_sampled": 10, "mean_faces": 0.0,
             "max_faces": 0, "face_frames_ratio": 0.0, "face_count_std": 0.0,
             "facecam_ratio": 0.0, "motion_mean": 0.1368,
             "motion_high_ratio": 1.0, "cut_ratio": 0.0, "mean_face_x": None}
    speech = {"speech_ratio": 0.0, "words_total": 0, "wpm": 0.0,
              "mean_turn_seconds": 0.0, "turns": 0, "short_turn_ratio": 0.0,
              "cut_off_ratio": 0.0, "questions": 0, "game_word_hits": 0,
              "convo_word_hits": 0, "narration_word_hits": 0,
              "transcript_chars": 0}
    return video, speech


def _narration_signals():
    video = {"opencv_available": True, "frames_sampled": 60, "mean_faces": 0.0,
             "max_faces": 0, "face_frames_ratio": 0.0, "face_count_std": 0.0,
             "facecam_ratio": 0.0, "motion_mean": 0.0429,
             "motion_high_ratio": 0.02, "cut_ratio": 0.0, "mean_face_x": None}
    speech = {"speech_ratio": 0.85, "words_total": 123, "wpm": 144.8,
              "mean_turn_seconds": 4.6, "turns": 11, "short_turn_ratio": 0.09,
              "cut_off_ratio": 0.27, "questions": 0, "game_word_hits": 1,
              "convo_word_hits": 1, "narration_word_hits": 7,
              "transcript_chars": 651}
    return video, speech


def test_classifier_returns_full_structure():
    r = classify_from_signals(*_gameplay_signals())
    assert r["category"] == "gameplay_streamer"
    assert r["subcategory"] == "gameplay_solo"
    assert 0.0 <= r["confidence"] <= 1.0
    assert isinstance(r["needs_user_choice"], bool)
    assert set(r["signals"]) == {"video", "speech", "scores"}
    assert set(r["signals"]["scores"]) == set(CATEGORIES)
    assert r["layout"]["layout"] == "gameplay_full"


def test_classifier_narration_is_not_podcast():
    # Una sola voz narrando, sin rostros y plano casi fijo: conversación,
    # subcategoría narración; NO podcast (que exige 2+ rostros estables).
    r = classify_from_signals(*_narration_signals())
    assert r["category"] == "conversacion_reaccion"
    assert r["subcategory"] == "narracion_solo"


def test_low_confidence_marks_needs_user_choice():
    # Señales ambiguas: poco movimiento, sin rostros, sin habla.
    video = {"opencv_available": True, "frames_sampled": 10, "mean_faces": 0.0,
             "max_faces": 0, "face_frames_ratio": 0.0, "face_count_std": 0.0,
             "facecam_ratio": 0.0, "motion_mean": 0.01,
             "motion_high_ratio": 0.0, "cut_ratio": 0.0, "mean_face_x": None}
    speech = {"speech_ratio": 0.0, "words_total": 0, "wpm": 0.0,
              "mean_turn_seconds": 0.0, "turns": 0, "short_turn_ratio": 0.0,
              "cut_off_ratio": 0.0, "questions": 0, "game_word_hits": 0,
              "convo_word_hits": 0, "narration_word_hits": 0,
              "transcript_chars": 0}
    r = classify_from_signals(video, speech)
    assert r["confidence"] < 0.6
    assert r["needs_user_choice"] is True


def test_manual_override_beats_auto():
    v = Video(title="t", category="gameplay_streamer",
              subcategory="gameplay_solo", classification_confidence=0.9,
              classification_source="auto", manual_override=False)
    assert effective_category(v) == ("gameplay_streamer", "auto")
    v.manual_override = True
    v.category = "podcast_entrevista"
    v.classification_source = "manual"
    assert effective_category(v) == ("podcast_entrevista", "manual")
    v2 = Video(title="t2")
    assert effective_category(v2) == ("otros", "default")


def test_score_weights_differ_by_category():
    seg = {"start": 0.0, "end": 45.0,
           "text": "¿Por qué nadie habla de esto? De repente todo cambió, "
                   "¡qué locura! ¿Tú qué opinas de esta regla?"}
    g = score_moment(seg, energy_peak=0.8, category="gameplay_streamer")
    p = score_moment(seg, energy_peak=0.8, category="podcast_entrevista")
    o = score_moment(seg, energy_peak=0.8, category="otros")
    assert g["breakdown"]["emocion"]["weight"] == CATEGORY_WEIGHTS["gameplay_streamer"]["emocion"]
    assert p["breakdown"]["curiosidad"]["weight"] == CATEGORY_WEIGHTS["podcast_entrevista"]["curiosidad"]
    assert g["breakdown"]["emocion"]["weight"] != p["breakdown"]["emocion"]["weight"]
    assert g["breakdown"]["curiosidad"]["weight"] != p["breakdown"]["curiosidad"]["weight"]
    assert g["category"] == "gameplay_streamer" and p["category"] == "podcast_entrevista"
    # El mismo segmento no puntúa igual en todas las categorías.
    assert len({g["score"], p["score"], o["score"]}) > 1
    # Bonus de interrupción/pregunta en podcast: frase cortada + pregunta.
    assert p["category_bonus"] > 0


def test_layout_for_category():
    assert layout_for_category("gameplay_streamer", {"facecam_ratio": 0.0})["layout"] == "gameplay_full"
    assert layout_for_category("gameplay_streamer", {"facecam_ratio": 0.5})["layout"] == "gameplay_facecam"
    face = layout_for_category("podcast_entrevista", {"mean_face_x": 0.3})
    assert face["layout"] == "face_focus" and face["focus_x"] == 0.3
    assert layout_for_category("otros")["layout"] == "center"


def _seed_video(db, **kw):
    v = Video(title=kw.get("title", "Test"), platform="upload",
              file_path=kw.get("file_path", "originals/test.mp4"))
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def test_patch_category_manual_override(db):
    v = _seed_video(db)
    r = client.patch(f"/api/videos/{v.id}/category",
                     json={"category": "podcast_entrevista"})
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "podcast_entrevista"
    assert body["subcategory"] == "podcast_estudio"
    assert body["manual_override"] is True
    assert body["classification_source"] == "manual"
    assert body["dashboard_button"] == BUTTONS["podcast_entrevista"]
    # La corrección manual queda guardada: GET la devuelve.
    assert client.get(f"/api/videos/{v.id}").json()["manual_override"] is True
    # Categoría inválida y subcategoría que no pertenece: 400.
    assert client.patch(f"/api/videos/{v.id}/category",
                        json={"category": "no_existe"}).status_code == 400
    assert client.patch(f"/api/videos/{v.id}/category",
                        json={"category": "gameplay_streamer",
                              "subcategory": "podcast_estudio"}).status_code == 400


def test_classify_endpoint_uses_auto_but_keeps_manual(db, monkeypatch):
    v = _seed_video(db)
    fake = {"category": "gameplay_streamer", "subcategory": "gameplay_solo",
            "confidence": 0.9, "needs_user_choice": False,
            "signals": {"video": {}, "speech": {}, "scores": {}},
            "layout": {"layout": "gameplay_full"}, "sample_seconds": 10.0,
            "duration_seconds": 10.0}
    monkeypatch.setattr("api.main.classify_video", lambda *a, **k: fake)
    monkeypatch.setattr("api.main.os.path.exists", lambda p: True)
    r = client.post(f"/api/videos/{v.id}/classify")
    assert r.status_code == 200
    assert r.json()["category"] == "gameplay_streamer"
    assert r.json()["saved"] is True
    # Corrección manual y reclasificación: la manual se conserva.
    client.patch(f"/api/videos/{v.id}/category", json={"category": "reality_casa"})
    r = client.post(f"/api/videos/{v.id}/classify")
    assert r.json()["saved"] is False
    assert r.json()["manual_override_kept"] is True
    assert client.get(f"/api/videos/{v.id}").json()["category"] == "reality_casa"


def test_render_with_face_focus_stays_1080x1920(tmp_path):
    src = str(tmp_path / "src.mp4")
    out = str(tmp_path / "clip.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", src], check=True)
    subs = [{"start": 0.0, "end": 1.5, "text": "HOLA"}]
    render_clip(src, 1.0, 4.0, subs, out, category="conversacion_reaccion",
                focus_x=0.25)
    info = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_streams",
         "-show_entries", "stream=codec_type,width,height",
         "-of", "json", out], text=True))
    video = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    assert (video["width"], video["height"]) == (1080, 1920)
