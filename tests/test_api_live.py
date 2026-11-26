"""API (TestClient, sin red) y gestor de directos."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from api.main import app
from viralclip.models import Clip
from worker.live import LiveManager

client = TestClient(app)


def _seed_clip(db, **kw):
    now = datetime.now(timezone.utc)
    c = Clip(title=kw.get("title", "Clip Nora"), platform=kw.get("platform", "twitch"),
             duration_seconds=kw.get("duration_seconds", 40.0), score=kw.get("score", 85.0),
             favorite=kw.get("favorite", False), file_path=kw.get("file_path", ""),
             thumbnail_path=kw.get("thumbnail_path", ""),
             expired=kw.get("expired", False),
             created_at=now, expires_at=now + timedelta(days=7))
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_validate_endpoint_honest_without_credentials():
    r = client.post("/api/streams/validate", json={"url": "https://twitch.tv/ibai"})
    assert r.status_code == 200
    assert r.json()["live_status"] == "requires_credentials"


def test_library_filters(db):
    _seed_clip(db, title="Casa noche", score=90.0, platform="twitch", favorite=True)
    _seed_clip(db, title="Otro tema", score=40.0, platform="youtube", favorite=False)
    assert len(client.get("/api/library").json()) >= 2
    fav = client.get("/api/library", params={"favorites_only": True}).json()
    assert all(c["favorite"] for c in fav) and len(fav) >= 1
    hi = client.get("/api/library", params={"min_score": 80}).json()
    assert all(c["score"] >= 80 for c in hi)
    by_name = client.get("/api/library", params={"q": "Casa noche"}).json()
    assert len(by_name) == 1 and by_name[0]["title"] == "Casa noche"
    by_platform = client.get("/api/library", params={"platform": "youtube"}).json()
    assert all(c["platform"] == "youtube" for c in by_platform)
    item = by_name[0]
    assert item["expires_at"] and item["seconds_remaining"] > 0


def test_favorite_toggle_and_delete(db):
    c = _seed_clip(db, title="Para borrar")
    r = client.post(f"/api/clips/{c.id}/favorite")
    assert r.json()["favorite"] is True
    r = client.post(f"/api/clips/{c.id}/favorite")
    assert r.json()["favorite"] is False
    r = client.delete(f"/api/clips/{c.id}")
    assert r.json()["deleted"] is True
    assert client.get("/api/library", params={"q": "Para borrar"}).json() == []


def test_thumbnail_endpoint(db):
    from viralclip.storage import get_storage

    storage = get_storage()
    storage.save("thumbs/1.jpg", __file__)  # bytes cualesquiera; el endpoint no valida el JPEG
    c = _seed_clip(db, title="Con miniatura", thumbnail_path="thumbs/1.jpg")
    r = client.get(f"/api/clips/{c.id}/thumbnail")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content  # se sirve el archivo


def test_thumbnail_missing_file_404(db):
    c = _seed_clip(db, title="Miniatura perdida", thumbnail_path="thumbs/noexiste.jpg")
    r = client.get(f"/api/clips/{c.id}/thumbnail")
    assert r.status_code == 404


def test_thumbnail_expired_410_and_no_thumbnail_410(db):
    c = _seed_clip(db, title="Caducado", thumbnail_path="thumbs/1.jpg", expired=True)
    assert client.get(f"/api/clips/{c.id}/thumbnail").status_code == 410
    c2 = _seed_clip(db, title="Sin miniatura", thumbnail_path="")
    assert client.get(f"/api/clips/{c2.id}/thumbnail").status_code == 410
    assert client.get("/api/clips/999999/thumbnail").status_code == 404


def test_quota_endpoint():
    r = client.get("/api/storage/quota")
    assert r.status_code == 200
    body = r.json()
    assert body["limit_gb"] == 10 and body["available_bytes"] <= body["limit_bytes"]
    assert body["retention_days"] == 7


def test_streams_create_and_stop(db):
    r = client.post("/api/streams", json={"url": "https://kick.com/westcol"})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert r.json()["platform"] == "kick"
    r = client.post(f"/api/streams/{sid}/stop")
    assert r.json()["status"] == "stopped"


def test_live_manager_limits():
    m = LiveManager(max_concurrent=3, default_active=1)
    a = m.add("twitch", "a", "https://twitch.tv/a")
    b = m.add("twitch", "b", "https://twitch.tv/b")
    c = m.add("twitch", "c", "https://twitch.tv/c")
    assert a.status == "live" and b.status == "waiting" and c.status == "waiting"
    assert m.active_count() == 1
    m.stop(a.id)
    assert m.get(b.id).status == "live"  # la cola promociona al siguiente
    assert m.get(c.id).status == "waiting"
