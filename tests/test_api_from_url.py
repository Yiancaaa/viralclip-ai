"""API: descarga por enlace, cookies de YouTube y proceso asíncrono."""
import time
from pathlib import Path

from fastapi.testclient import TestClient

import api.main as api_main
from api.main import app
from viralclip.models import Video

client = TestClient(app)
URL = "https://www.youtube.com/watch?v=Dxi1CRQMQZo"
COOKIES_TXT = ("# Netscape HTTP Cookie File\n"
               ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\txyz\n"
               ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tHSID\tabc\n")


def _fake_download(url, dest_dir, cookies_path=None, **kw):
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    p = dest / "Dxi1CRQMQZo.mp4"
    p.write_bytes(b"FAKEVIDEO" * 200)
    assert cookies_path is None or str(cookies_path).endswith(".txt")
    return {"path": str(p), "title": "La casa que solo existe de noche",
            "duration_seconds": 107, "video_id": "Dxi1CRQMQZo",
            "size_bytes": p.stat().st_size}


def _wait_job(job_id, timeout=20):
    for _ in range(int(timeout / 0.5)):
        r = client.get(f"/api/jobs/{job_id}")
        if r.status_code == 200 and r.json()["status"] in ("done", "error"):
            return r.json()
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} no terminó en {timeout}s")


def test_from_url_rejects_non_youtube():
    r = client.post("/api/videos/from-url", json={"url": "https://twitch.tv/ibai"})
    assert r.status_code == 400
    r = client.post("/api/videos/from-url",
                    json={"url": "https://www.youtube.com/@Yiancaaa"})
    assert r.status_code == 400


def test_from_url_downloads_and_creates_video(monkeypatch, db):
    monkeypatch.setattr(api_main, "download_video_url", _fake_download)
    r = client.post("/api/videos/from-url",
                    json={"url": URL, "auto_process": False})
    assert r.status_code == 202
    job = _wait_job(r.json()["job_id"])
    assert job["status"] == "done"
    assert job["video_id"]
    v = db.query(Video).filter(Video.id == job["video_id"]).one()
    assert v.platform == "youtube"
    assert v.source_url == URL
    assert v.title == "La casa que solo existe de noche"
    assert v.duration_seconds == 107
    # Segunda vez con el mismo enlace: duplicado por source_url.
    r2 = client.post("/api/videos/from-url",
                     json={"url": URL, "auto_process": False})
    assert r2.status_code == 202
    assert r2.json()["status"] == "duplicate"
    assert r2.json()["video_id"] == v.id


def test_from_url_blocked_error_is_clear(monkeypatch):
    from worker.download import DownloadError

    def _blocked(url, dest_dir, cookies_path=None, **kw):
        raise DownloadError(
            "blocked",
            "YouTube bloqueó la descarga desde este servidor. Sube tus cookies.")
    monkeypatch.setattr(api_main, "download_video_url", _blocked)
    r = client.post("/api/videos/from-url",
                    json={"url": "https://youtu.be/otroVideo1",
                          "auto_process": False})
    assert r.status_code == 202
    job = _wait_job(r.json()["job_id"])
    assert job["status"] == "error"
    assert "cookies" in job["error"]


def test_cookies_upload_status_delete():
    # Sin cookies al principio (o de otro test, se borran al final).
    client.delete("/api/cookies/youtube")
    assert client.get("/api/cookies/youtube/status").json() == {"configured": False}
    r = client.post("/api/cookies/youtube",
                    files={"file": ("cookies.txt", COOKIES_TXT.encode(), "text/plain")})
    assert r.status_code == 200
    assert r.json()["uploaded"] is True
    assert r.json()["youtube_cookies"] == 2
    st = client.get("/api/cookies/youtube/status").json()
    assert st["configured"] is True
    assert st["youtube_cookies"] == 2
    assert st["uploaded_at"]
    r = client.delete("/api/cookies/youtube")
    assert r.json()["deleted"] is True
    assert client.get("/api/cookies/youtube/status").json() == {"configured": False}


def test_cookies_upload_rejects_file_without_youtube():
    r = client.post("/api/cookies/youtube",
                    files={"file": ("cookies.txt", b".google.com\tTRUE\t/\tTRUE\t0\tNID\tx\n",
                                    "text/plain")})
    assert r.status_code == 400
    assert "youtube.com" in r.json()["detail"]


def test_process_returns_202_and_job_ends(db):
    v = Video(title="Falso", platform="upload", file_path="originals/noexiste.mp4",
              file_size_bytes=0, sha256="abc")
    db.add(v)
    db.commit()
    db.refresh(v)
    r = client.post(f"/api/videos/{v.id}/process")
    assert r.status_code == 202
    job = _wait_job(r.json()["job_id"])
    # El archivo no existe: el job termina en error, pero el endpoint
    # ya respondió 202 al instante (sin 502).
    assert job["status"] == "error"
    assert job["error"]
