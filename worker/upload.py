"""Subida directa de archivos a S3 con URL prefirmada.

Problema que resuelve (verificado en vivo 2026-11-26): el endpoint
clásico `POST /api/videos/upload` lee el archivo ENTERO en RAM
(`await file.read()`) y la petición pasa por el proxy de Render. La
subida de un vídeo del usuario (descargado de YouTube Studio) llevaba
1 hora y nunca llegó al servidor: no existía vídeo nuevo y el
almacenamiento usado era 0 bytes.

Flujo nuevo (sin tocar el endpoint clásico, que queda como reserva
para archivos pequeños):
  1. El navegador calcula el SHA-256 del archivo y su duración.
  2. `POST /api/videos/upload/presign` valida (tamaño, SHA, extensión,
     duplicado, cuota) y devuelve una URL prefirmada de PUT.
  3. El navegador sube el archivo DIRECTO a S3 con esa URL (con barra
     de progreso real); el proceso web no ve ni un byte.
  4. `POST /api/videos/upload/complete` verifica que el objeto existe
     en S3 y que su tamaño coincide con el declarado, y registra el
     vídeo en la BD (con deduplicación por SHA-256).

Limitaciones honestas:
  - El SHA-256 lo calcula el navegador y el servidor lo usa para la
    deduplicación y para derivar la clave; el servidor NO vuelve a
    hashear el archivo (eso exigiría descargarlo entero, justo lo que
    se evita). La integridad se comprueba por tamaño (head_object) y
    el proceso posterior fallaría de forma visible si el archivo
    estuviera corrupto.
  - Un PUT prefirmado real a S3 necesita credenciales vivas y CORS del
    bucket hacia el origen del panel; en local solo se verifica con
    fakes (ver tests/test_upload_direct.py). Si el PUT directo falla
    (p. ej. CORS), el panel usa la subida clásica como reserva.
"""
from __future__ import annotations

import re
from pathlib import Path

from viralclip.models import Video

# Extensiones de vídeo aceptadas (minúsculas, con punto).
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

# Validez de la URL prefirmada: 6 h, para que una subida lenta de un
# archivo grande no caduque a mitad. SigV4 permite hasta 7 días.
PRESIGNED_URL_EXPIRES_SECONDS = 6 * 3600

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^originals/[0-9a-f]{12}\.(mp4|mov|mkv|webm|avi|m4v)$")


class UploadTooLargeError(ValueError):
    """El archivo supera MAX_UPLOAD_GB (la API lo traduce a HTTP 413)."""


def build_upload_key(sha256: str, suffix: str) -> str:
    """Clave en almacenamiento para un original subido.

    Igual que la del endpoint clásico: `originals/{sha[:12]}{suffix}`.
    """
    return f"originals/{sha256[:12]}{suffix.lower()}"


def validate_upload_request(filename: str, size_bytes: int, sha256: str,
                            duration_seconds: float = 0.0,
                            max_size_bytes: int = 2 * 1024 ** 3,
                            max_duration_seconds: float = 7200) -> str:
    """Valida una petición de subida directa.

    Devuelve el sufijo normalizado (p. ej. ".mp4"). Lanza ValueError
    con un mensaje claro si algo no es válido.
    """
    suffix = Path(filename or "").suffix.lower()
    if not suffix or suffix not in ALLOWED_VIDEO_EXTENSIONS:
        raise ValueError(
            f"Extensión '{suffix or ''}' no admitida; usa una de: "
            f"{', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS))}.")
    if not isinstance(size_bytes, int) or size_bytes <= 0:
        raise ValueError("size_bytes debe ser un entero mayor que 0.")
    if size_bytes > max_size_bytes:
        raise UploadTooLargeError(
            f"El archivo pesa {size_bytes / 1024 ** 3:.2f} GB; el máximo "
            f"es {max_size_bytes / 1024 ** 3:.0f} GB.")
    if not _SHA256_RE.match(sha256 or ""):
        raise ValueError("sha256 debe ser un hash SHA-256 hexadecimal de 64 caracteres.")
    if duration_seconds is None:
        duration_seconds = 0.0
    if duration_seconds < 0 or duration_seconds > max_duration_seconds:
        raise ValueError(
            f"duration_seconds fuera de rango (0–{max_duration_seconds:.0f} s).")
    return suffix


def presign_direct_upload(storage, key: str, content_type: str = "video/mp4") -> str:
    """URL prefirmada de PUT para subir directo a `key`.

    Lanza NotImplementedError si el backend no lo soporta (local).
    """
    return storage.presigned_put_url(key, content_type=content_type,
                                     expires_in=PRESIGNED_URL_EXPIRES_SECONDS)


def register_uploaded_video(db, storage, *, key: str, sha256: str,
                            filename: str, title: str = "",
                            size_bytes: int = 0,
                            duration_seconds: float = 0.0):
    """Registra en BD un vídeo ya subido directo a S3.

    Comprueba: la clave es exactamente la derivada del SHA-256 y la
    extensión del archivo (nada de claves arbitrarias), el objeto
    existe en almacenamiento y su tamaño coincide con el declarado
    (detecta subidas truncadas). Deduplica por SHA-256 como el
    endpoint clásico.

    Devuelve (video, is_duplicate). No crea duplicados.
    """
    suffix = validate_upload_request(filename, size_bytes, sha256,
                                     duration_seconds)
    if not _KEY_RE.match(key or "") or key != build_upload_key(sha256, suffix):
        raise ValueError(
            "key no válida: debe ser originals/{sha256[:12]}{extensión} "
            "derivada del SHA-256 declarado.")
    if not storage.exists(key):
        raise FileNotFoundError(
            "El archivo no está en el almacenamiento; la subida directa "
            "no se completó (vuelve a subirlo).")
    stored_size = storage.size_bytes(key)
    if stored_size != size_bytes:
        raise ValueError(
            f"El archivo en almacenamiento pesa {stored_size} bytes, no "
            f"{size_bytes}: subida incompleta o truncada.")
    dup = db.query(Video).filter(Video.sha256 == sha256).first()
    if dup:
        return dup, True
    v = Video(title=title or filename, platform="upload", file_path=key,
              file_size_bytes=size_bytes, sha256=sha256,
              duration_seconds=float(duration_seconds or 0.0))
    db.add(v)
    db.commit()
    db.refresh(v)
    return v, False
