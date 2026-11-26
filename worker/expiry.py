"""Caducidad: borra clips y miniaturas cuya expires_at pasó (7 días desde
created_at), libera espacio y conserva solo metadatos básicos.

Reglas (decisión del usuario 2026-11-26):
- Los clips FAVORITOS están EXENTOS del borrado: nunca se borran por
  caducidad, aunque su expires_at ya haya pasado.
- La expiración avisa 24 h antes: `clips_expiring_soon` lista los clips
  que caducan en menos de 24 h para que el panel lo muestre, y
  `time_remaining` marca `expiring_soon` en cada clip.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from viralclip.models import Clip
from viralclip.storage import Storage

# Horas antes de la caducidad en las que el panel debe avisar.
EXPIRY_WARNING_HOURS = 24


def expire_clips(db: Session, storage: Storage, now: datetime | None = None) -> dict:
    """Marca como expired los clips vencidos, borra sus archivos del storage
    y conserva la fila con metadatos básicos (título, puntuación, fechas).
    Los clips favoritos se saltan: no se borran nunca por caducidad.
    Devuelve resumen real de lo hecho."""
    now = now or datetime.now(timezone.utc)
    expired = db.query(Clip).filter(Clip.expires_at <= now, Clip.expired.is_(False),
                                    Clip.favorite.is_(False)).all()
    skipped = db.query(Clip).filter(Clip.expires_at <= now, Clip.expired.is_(False),
                                    Clip.favorite.is_(True)).all()
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
            "freed_bytes": freed_bytes, "clip_ids": [c.id for c in expired],
            "skipped_favorite_count": len(skipped),
            "skipped_favorite_ids": [c.id for c in skipped]}


def clips_expiring_soon(db: Session, within_hours: int = EXPIRY_WARNING_HOURS,
                        now: datetime | None = None) -> list[dict]:
    """Clips que caducan en menos de `within_hours` (por defecto 24 h).

    Solo clips vivos y NO favoritos: un favorito nunca caduca, así que
    tampoco avisa. Los ya vencidos no se listan aquí: les toca
    expire_clips, no el aviso. Devuelve un dict por clip con lo que el
    panel necesita para mostrar el aviso (id, título, expiración,
    tiempo restante), ordenados por expiración.
    """
    now = now or datetime.now(timezone.utc)
    limit = now + timedelta(hours=within_hours)
    clips = (db.query(Clip).filter(Clip.expired.is_(False), Clip.favorite.is_(False),
                                   Clip.expires_at > now, Clip.expires_at <= limit)
             .order_by(Clip.expires_at.asc()).all())
    out = []
    for clip in clips:
        remaining = time_remaining(clip, now=now)
        out.append({"clip_id": clip.id, "title": clip.title,
                    "expires_at": remaining["expires_at"],
                    "seconds_remaining": remaining["seconds_remaining"],
                    "hours_remaining": round(remaining["seconds_remaining"] / 3600, 1)})
    return out


def time_remaining(clip: Clip, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    expires = clip.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    delta = (expires - now).total_seconds()
    seconds = max(0, int(delta))
    return {"expires_at": expires.isoformat(), "seconds_remaining": seconds,
            "expired": delta <= 0 or bool(clip.expired),
            "favorite_exempt": bool(clip.favorite),
            "expiring_soon": (not clip.expired and not clip.favorite
                              and 0 < delta <= EXPIRY_WARNING_HOURS * 3600)}
