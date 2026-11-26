"""Descarga de vídeos propios desde un enlace de YouTube con yt-dlp.

Solo contenido propio/autorizado: el usuario pega el enlace de SU vídeo
y la plataforma lo descarga para procesarlo, como OpusClip/NexusClips.
YouTube NO entrega el archivo de vídeo por ninguna API oficial, así que
la descarga usa yt-dlp. Hecho medido (2026-11-26): sin sesión, YouTube
bloquea las descargas desde servidores en la nube («Sign in to confirm
you're not a bot», HTTP 429). Por eso la descarga admite un archivo de
cookies de YouTube del propio usuario (exportado desde su navegador):
con esas cookies yt-dlp descarga como si fuera él. Sin cookies, el
error es claro (código ``blocked``) y el panel explica cómo subirlas.

Límites: solo URLs de vídeos de YouTube, duración máxima configurable
y tamaño máximo = MAX_UPLOAD_GB. Nunca se descargan listas de
reproducción completas (noplaylist).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Patrón propio de vídeos de YouTube (watch/live/shorts/embed/youtu.be).
# No se reutiliza detect_platform: ese también acepta URLs de canal
# (@...) y aquí solo valen vídeos concretos.
_YT_VIDEO_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^#]*v=|live/|shorts/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_\-]{6,})"
)

# Formato: mejor vídeo ≤1080p en MP4 + mejor audio M4A; si no hay MP4,
# mejor vídeo ≤1080p + audio; si el vídeo solo existe como DASH (sin
# formato combinado) o es vertical con altura >1080p (Shorts), el último
# recurso sin filtros descarga lo mejor disponible. merge_output_format
# =mp4 deja el archivo listo para el pipeline. (2026-11-26: el vídeo
# zfdmlW4tYjI de Juan falló con "Requested format is not available"
# porque ningún selector con filtros casaba con sus formatos.)
FORMAT = ("bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
          "bestvideo[height<=1080]+bestaudio/"
          "best[height<=1080][ext=mp4]/best/"
          "bestvideo+bestaudio/best")
MAX_DURATION_SECONDS = 7200  # 2 h por defecto (configurable por settings)
MAX_SIZE_BYTES = 2 * 1024 ** 3  # 2 GB por defecto (= MAX_UPLOAD_GB)


class DownloadError(Exception):
    """Error de descarga con código legible para el panel.

    Códigos: ``blocked`` (YouTube bloqueó la descarga desde el servidor;
    hacen falta cookies), ``unavailable`` (vídeo privado/eliminado/no
    accesible), ``too_long``, ``too_big``, ``failed`` (otros).
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def is_youtube_video_url(url: str) -> tuple[bool, str | None]:
    """True + id del vídeo si la URL es un vídeo de YouTube (watch, live,
    shorts, embed o youtu.be). Las URLs de canal (@...) no valen."""
    m = _YT_VIDEO_RE.search((url or "").strip())
    if not m:
        return False, None
    return True, m.group(1)


def _classify_error(msg: str) -> tuple[str, str]:
    low = msg.lower()
    # Primero la restricción de edad: «Sign in to confirm your age»
    # también contiene «sign in to confirm» y no es un bloqueo de bot.
    if ("private video" in low or "video unavailable" in low
            or "this video is no longer" in low or "removed" in low
            or "confirm your age" in low or "age-restricted" in low
            or "age verification" in low):
        return ("unavailable",
                "El vídeo no está disponible (privado, eliminado o restringido).")
    if ("sign in to confirm" in low or "not a bot" in low or "429" in low
            or "too many requests" in low or "confirm you're not a robot" in low):
        return ("blocked",
                "YouTube bloqueó la descarga desde este servidor "
                "(«confirma que no eres un robot»). Sube tus cookies de "
                "YouTube en el panel y vuelve a intentarlo.")
    return ("failed", f"Descarga fallida: {msg[:500]}")


def _ydl_opts(cookies_path: str | None, outtmpl: str | None = None) -> dict:
    # Clientes de reproductor: primero las apps (Android/iOS, vía la API
    # innertube), y la web solo como último recurso. Medido 2026-11-26:
    # desde servidores en la nube el cliente web devuelve la página de
    # «confirma que no eres un robot» (o una respuesta sin formatos, que
    # se manifiesta como «Requested format is not available» aunque el
    # selector tenga el último recurso «best»), incluso con cookies
    # válidas (jobs 5 y 8 con zfdmlW4tYjI). El cliente Android sí
    # devuelve formatos (itag 18, 640x360) para ese mismo vídeo.
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": FORMAT,
        "merge_output_format": "mp4",
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "restrictfilenames": True,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
    }
    if cookies_path:
        opts["cookiefile"] = cookies_path
    if outtmpl:
        opts["outtmpl"] = outtmpl
    return opts


def extract_info(url: str, cookies_path: str | None = None) -> dict:
    """Metadatos del vídeo sin descargarlo. Lanza DownloadError si
    YouTube bloquea o el vídeo no existe."""
    ok, video_id = is_youtube_video_url(url)
    if not ok:
        raise DownloadError("failed",
                            "Solo se admiten enlaces de vídeos de YouTube "
                            "(watch?v=..., youtu.be/..., shorts/...).")
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL(_ydl_opts(cookies_path)) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError:
        raise
    except Exception as e:  # noqa: BLE001 - yt-dlp levanta DownloadError
        code, msg = _classify_error(str(e))
        raise DownloadError(code, msg) from e
    if not info:
        raise DownloadError("unavailable", "No se pudo leer la información del vídeo.")
    return {
        "video_id": info.get("id") or video_id,
        "title": info.get("title") or "",
        "duration_seconds": float(info.get("duration") or 0),
        "uploader": info.get("uploader") or "",
        "filesize_approx": info.get("filesize_approx") or info.get("filesize"),
    }


def download_video_url(url: str, dest_dir: str | Path,
                       cookies_path: str | None = None,
                       max_duration_s: int = MAX_DURATION_SECONDS,
                       max_size_bytes: int = MAX_SIZE_BYTES) -> dict:
    """Descarga el vídeo y devuelve {path, title, duration_seconds,
    video_id, size_bytes}. Comprueba duración y tamaño antes de bajar."""
    info = extract_info(url, cookies_path)
    duration = info["duration_seconds"]
    if duration and duration > max_duration_s:
        raise DownloadError(
            "too_long",
            f"El vídeo dura {duration / 60:.0f} minutos; el máximo es "
            f"{max_duration_s / 60:.0f} minutos.")
    approx = info.get("filesize_approx")
    if approx and approx > max_size_bytes:
        raise DownloadError(
            "too_big",
            f"El vídeo pesa ~{approx / 1024 ** 2:.0f} MB; el máximo es "
            f"{max_size_bytes / 1024 ** 3:.0f} GB.")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    outtmpl = str(dest / "%(id)s.%(ext)s")
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL(_ydl_opts(cookies_path, outtmpl)) as ydl:
            ydl.download([url])
    except DownloadError:
        raise
    except Exception as e:  # noqa: BLE001
        code, msg = _classify_error(str(e))
        raise DownloadError(code, msg) from e
    # yt-dlp guarda %(id)s.%(ext)s; buscamos el archivo resultante.
    candidates = sorted(dest.glob(f"{info['video_id']}.*"))
    if not candidates:
        # Por si el id real difiere del de la URL (p. ej. shorts).
        files = [p for p in dest.iterdir() if p.is_file()]
        if not files:
            raise DownloadError("failed", "La descarga terminó sin archivo.")
        candidates = sorted(files, key=lambda p: p.stat().st_mtime)
    path = candidates[-1]
    size = path.stat().st_size
    if size > max_size_bytes:
        path.unlink(missing_ok=True)
        raise DownloadError(
            "too_big",
            f"El vídeo pesa {size / 1024 ** 2:.0f} MB; el máximo es "
            f"{max_size_bytes / 1024 ** 3:.0f} GB.")
    if size == 0:
        path.unlink(missing_ok=True)
        raise DownloadError("failed", "La descarga quedó vacía (0 bytes).")
    return {"path": str(path), "title": info["title"],
            "duration_seconds": duration, "video_id": info["video_id"],
            "size_bytes": size}
