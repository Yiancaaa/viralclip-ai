"""Subida directa a S3 con URL prefirmada (POST /api/videos/upload/presign
y /complete) y GET /api/videos. El storage se falsifica: ningún test
toca S3 real ni necesita credenciales vivas."""
import hashlib

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.main import app
from viralclip.models import Job, Video
from worker.upload import (PRESIGNED_URL_EXPIRES_SECONDS, build_upload_key,
                           validate_upload_request)

client = TestClient(app)
MAX_SIZE = 2 * 1024 ** 3


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


class FakeStorage:
    """Imita el storage S3 con objetos en memoria + presigned URL."""

    def __init__(self, presign_raises=None):
        self.objects: dict[str, bytes] = {}
        self.presigned_calls: list[tuple] = []
        self.presign_raises = presign_raises

    def save(self, key, src_path):
        with open(src_path, "rb") as f:
            self.objects[key] = f.read()
        return key

    def read(self, key):
        return self.objects[key]

    def delete(self, key):
        return self.objects.pop(key, None) is not None

    def exists(self, key):
        return key in self.objects

    def size_bytes(self, key):
        return len(self.objects.get(key, b""))

    def presigned_put_url(self, key, content_type="video/mp4", expires_in=3600):
        if self.presign_raises is not None:
            raise self.presign_raises
        self.presigned_calls.append((key, content_type, expires_in))
        return f"https://fake-s3.example/bucket/{key}?X-Amz-Signature=fake"


@pytest.fixture()
def fake_storage(monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr(api_main, "get_storage", lambda: fake)
    return fake


def _presign_body(seed="video-1", size=12345, filename="mi_video.mp4",
                  title="Mi vídeo", duration=107.0):
    return {"filename": filename, "size_bytes": size, "sha256": _sha(seed),
            "title": title, "duration_seconds": duration,
            "content_type": "video/mp4"}


# ---------------------------------------------------------------- presign
def test_presign_ok(fake_storage):
    r = client.post("/api/videos/upload/presign", json=_presign_body())
    assert r.status_code == 200, r.text
    data = r.json()
    sha = _sha("video-1")
    assert data["key"] == f"originals/{sha[:12]}.mp4"
    assert data["upload_url"].startswith("https://fake-s3.example/bucket/")
    assert data["upload_url"].endswith(data["key"] + "?X-Amz-Signature=fake")
    assert data["expires_in"] == PRESIGNED_URL_EXPIRES_SECONDS
    assert data["content_type"] == "video/mp4"
    assert fake_storage.presigned_calls == [
        (data["key"], "video/mp4", PRESIGNED_URL_EXPIRES_SECONDS)]


def test_presign_rejects_oversize(fake_storage):
    r = client.post("/api/videos/upload/presign",
                    json=_presign_body(size=MAX_SIZE + 1))
    assert r.status_code == 413
    assert "2 GB" in r.json()["detail"]


def test_presign_rejects_bad_sha(fake_storage):
    body = _presign_body()
    body["sha256"] = "no-es-un-sha"
    r = client.post("/api/videos/upload/presign", json=body)
    assert r.status_code == 400
    assert "SHA-256" in r.json()["detail"]


def test_presign_rejects_bad_extension(fake_storage):
    r = client.post("/api/videos/upload/presign",
                    json=_presign_body(filename="video.exe"))
    assert r.status_code == 400
    assert "no admitida" in r.json()["detail"]


def test_presign_duplicate_skips_upload(fake_storage, db):
    sha = _sha("ya-existe")
    v = Video(title="Ya existe", platform="upload", file_path="originals/abc.mp4",
              file_size_bytes=10, sha256=sha)
    db.add(v)
    db.commit()
    db.refresh(v)
    r = client.post("/api/videos/upload/presign", json=_presign_body(seed="ya-existe"))
    assert r.status_code == 200
    assert r.json()["duplicate"] is True
    assert r.json()["video_id"] == v.id
    assert "upload_url" not in r.json()
    assert fake_storage.presigned_calls == []


def test_presign_local_backend_returns_501(monkeypatch):
    fake = FakeStorage(presign_raises=NotImplementedError(
        "presigned_put_url no disponible en almacenamiento local"))
    monkeypatch.setattr(api_main, "get_storage", lambda: fake)
    r = client.post("/api/videos/upload/presign", json=_presign_body(seed="local-1"))
    assert r.status_code == 501
    assert "almacenamiento local" in r.json()["detail"]


# ---------------------------------------------------------------- complete
def test_complete_registers_video(fake_storage, db):
    sha = _sha("completo-1")
    key = build_upload_key(sha, ".mp4")
    fake_storage.objects[key] = b"V" * 12345
    r = client.post("/api/videos/upload/complete", json={
        "key": key, "sha256": sha, "filename": "mi_video.mp4",
        "title": "Mi vídeo", "size_bytes": 12345, "duration_seconds": 107.0})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"]
    assert data["platform"] == "upload"
    assert data["file_path"] == key
    assert data["file_size_bytes"] == 12345
    assert data["duration_seconds"] == 107.0
    assert data["duplicate"] is False
    v = db.query(Video).filter(Video.id == data["id"]).one()
    assert v.sha256 == sha
    assert v.title == "Mi vídeo"


def test_complete_missing_object(fake_storage):
    sha = _sha("falta-1")
    key = build_upload_key(sha, ".mp4")
    r = client.post("/api/videos/upload/complete", json={
        "key": key, "sha256": sha, "filename": "mi_video.mp4",
        "title": "Mi vídeo", "size_bytes": 100, "duration_seconds": 0})
    assert r.status_code == 404
    assert "no está en el almacenamiento" in r.json()["detail"]


def test_complete_size_mismatch(fake_storage, db):
    sha = _sha("corto-1")
    key = build_upload_key(sha, ".mp4")
    fake_storage.objects[key] = b"V" * 50  # se declararon 12345
    r = client.post("/api/videos/upload/complete", json={
        "key": key, "sha256": sha, "filename": "mi_video.mp4",
        "title": "Mi vídeo", "size_bytes": 12345, "duration_seconds": 0})
    assert r.status_code == 400
    assert "incompleta" in r.json()["detail"]
    assert db.query(Video).filter(Video.sha256 == sha).first() is None


def test_complete_rejects_arbitrary_key(fake_storage, db):
    sha = _sha("clave-mala")
    fake_storage.objects["originals/aaaaaaaaaaaa.mp4"] = b"V" * 10
    r = client.post("/api/videos/upload/complete", json={
        "key": "originals/aaaaaaaaaaaa.mp4", "sha256": sha,
        "filename": "mi_video.mp4", "title": "Mi vídeo",
        "size_bytes": 10, "duration_seconds": 0})
    assert r.status_code == 400
    assert db.query(Video).filter(Video.sha256 == sha).first() is None


def test_complete_dedup_second_time(fake_storage, db):
    sha = _sha("dup-1")
    key = build_upload_key(sha, ".mp4")
    fake_storage.objects[key] = b"V" * 777
    body = {"key": key, "sha256": sha, "filename": "mi_video.mp4",
            "title": "Mi vídeo", "size_bytes": 777, "duration_seconds": 10}
    r1 = client.post("/api/videos/upload/complete", json=body)
    assert r1.status_code == 200
    assert r1.json()["duplicate"] is False
    r2 = client.post("/api/videos/upload/complete", json=body)
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True
    assert r2.json()["id"] == r1.json()["id"]
    assert db.query(Video).filter(Video.sha256 == sha).count() == 1


# ---------------------------------------------------------------- GET /api/videos
def test_list_videos_most_recent_first_with_process_status(fake_storage, db):
    v1 = Video(title="Primero", platform="upload", file_path="originals/a.mp4",
               file_size_bytes=1, sha256=_sha("lista-1"))
    v2 = Video(title="Segundo", platform="upload", file_path="originals/b.mp4",
               file_size_bytes=2, sha256=_sha("lista-2"))
    db.add_all([v1, v2])
    db.commit()
    db.refresh(v1)
    db.refresh(v2)
    db.add(Job(video_id=v2.id, kind="process", status="done"))
    db.commit()
    r = client.get("/api/videos")
    assert r.status_code == 200
    items = [x for x in r.json() if x["id"] in (v1.id, v2.id)]
    assert [x["id"] for x in items] == [v2.id, v1.id]  # más reciente primero
    by_id = {x["id"]: x for x in items}
    assert by_id[v2.id]["process_status"] == "done"
    assert by_id[v2.id]["latest_process_job_id"] is not None
    assert by_id[v2.id]["clips_count"] == 0
    assert by_id[v1.id]["process_status"] == "sin_procesar"
    assert by_id[v1.id]["latest_process_job_id"] is None


# ---------------------------------------------------------------- lógica pura
def test_build_upload_key():
    sha = "a" * 64
    assert build_upload_key(sha, ".MP4") == "originals/aaaaaaaaaaaa.mp4"


def test_validate_upload_request_errors():
    with pytest.raises(ValueError, match="no admitida"):
        validate_upload_request("video.exe", 10, "a" * 64)
    with pytest.raises(ValueError, match="mayor que 0"):
        validate_upload_request("video.mp4", 0, "a" * 64)
    with pytest.raises(ValueError, match="máximo"):
        validate_upload_request("video.mp4", MAX_SIZE + 1, "a" * 64,
                                max_size_bytes=MAX_SIZE)
    with pytest.raises(ValueError, match="SHA-256"):
        validate_upload_request("video.mp4", 10, "xyz")
    with pytest.raises(ValueError, match="fuera de rango"):
        validate_upload_request("video.mp4", 10, "a" * 64,
                                duration_seconds=99999)
    assert validate_upload_request("VIDEO.MP4", 10, "a" * 64) == ".mp4"
