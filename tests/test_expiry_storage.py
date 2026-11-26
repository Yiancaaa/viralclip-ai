"""Expiración a 7 días y almacenamiento local (guardar/leer/borrar)."""
from datetime import datetime, timedelta, timezone

import pytest

from viralclip.models import Clip
from viralclip.storage import LocalStorage
from worker.expiry import expire_clips, time_remaining


def test_local_storage_roundtrip(tmp_path):
    s = LocalStorage(tmp_path / "store")
    src = tmp_path / "a.bin"
    src.write_bytes(b"hello viralclip")
    key = s.save("clips/1.mp4", str(src))
    assert s.exists(key)
    assert s.read(key) == b"hello viralclip"
    assert s.size_bytes(key) == len(b"hello viralclip")
    assert s.delete(key) is True
    assert not s.exists(key)
    assert s.delete(key) is False


def test_local_storage_rejects_traversal(tmp_path):
    s = LocalStorage(tmp_path / "store")
    src = tmp_path / "a.bin"
    src.write_bytes(b"x")
    with pytest.raises(ValueError):
        s.save("../escape.mp4", str(src))


def test_expiry_seven_days(db, tmp_path):
    storage = LocalStorage(tmp_path / "store")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"0" * 1024)
    storage.save("clips/1.mp4", str(src))
    storage.save("thumbs/1.jpg", str(src))
    now = datetime.now(timezone.utc)
    old = Clip(title="viejo", platform="twitch", duration_seconds=30.0, score=80.0,
               file_path="clips/1.mp4", thumbnail_path="thumbs/1.jpg", file_size_bytes=1024,
               created_at=now - timedelta(days=8), expires_at=now - timedelta(days=1))
    fresh = Clip(title="nuevo", platform="twitch", duration_seconds=30.0, score=70.0,
                 file_path="clips/2.mp4", thumbnail_path="", file_size_bytes=0,
                 created_at=now, expires_at=now + timedelta(days=7))
    db.add_all([old, fresh])
    db.commit()
    result = expire_clips(db, storage, now=now)
    assert result["expired_count"] == 1
    assert result["clip_ids"] == [old.id]
    assert result["freed_bytes"] == 2048
    assert not storage.exists("clips/1.mp4")
    # Metadatos básicos conservados, archivo eliminado
    db.refresh(old)
    assert old.expired is True and old.file_path == "" and old.title == "viejo" and old.score == 80.0
    db.refresh(fresh)
    assert fresh.expired is False


def test_time_remaining():
    now = datetime.now(timezone.utc)
    c = Clip(created_at=now, expires_at=now + timedelta(days=7))
    tr = time_remaining(c, now=now)
    assert tr["seconds_remaining"] == 7 * 86400
    assert tr["expired"] is False
