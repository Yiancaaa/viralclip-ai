"""Caducidad: borra clips y miniaturas cuya expires_at pasó (7 días desde
created_at), libera espacio y conserva solo metadatos básicos."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from viralclip.models import Clip
from viralclip.storage import Storage


def expire_clips(db: Session, storage: Storage, now: datetime | None = None) -> dict:
    """Marca como expired los clips vencidos, borra sus archivos del storage
    y conserva la fila con metadatos básicos (título, puntuación, fechas).
    Devuelve resumen real de lo hecho."""
    now = now or datetime.now(timezone.utc)
    expired = db.query(Clip).filter(Clip.expires_at <= now, Clip.expired.is_(False)).all()
    freed_bytes = 0
    deleted_files = 0
    for clip in expired:
        for key in (clip.file_path, clip.thumbnail_path):
            if key:
                freed_bytes += storage.size_bytes(key)
                if storage.delete(key):
                    deleted_files += 1
        clip.expired = True
        clip.file_path = ""
        clip.thumbnail_path = ""
    db.commit()
    return {"expired_count": len(expired), "deleted_files": deleted_files,
            "freed_bytes": freed_bytes, "clip_ids": [c.id for c in expired]}


def time_remaining(clip: Clip, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    expires = clip.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    delta = (expires - now).total_seconds()
    return {"expires_at": expires.isoformat(), "seconds_remaining": max(0, int(delta)),
            "expired": delta <= 0 or bool(clip.expired)}
