"""Detección de plataforma y validación de URLs de directos.

Regla de honestidad: si no hay credenciales para consultar el estado en
vivo, el resultado es `requires_credentials` / `not_verifiable`, NUNCA un
estado en vivo inventado. Ver docs/CONECTORES.md.
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
import json

PLATFORMS = ("twitch", "kick", "youtube")

_TWITCH_RE = re.compile(r"(?:https?://)?(?:www\.)?twitch\.tv/([A-Za-z0-9_]+)/?$")
_KICK_RE = re.compile(r"(?:https?://)?(?:www\.)?kick\.com/([A-Za-z0-9_\-]+)/?$")
_YT_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:watch\?[^#]*v=|live/|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_\-]{6,})"
)
_YT_CHANNEL_RE = re.compile(r"(?:https?://)?(?:www\.)?youtube\.com/(?:@|channel/|c/)([A-Za-z0-9_\-\.]+)/?$")


def detect_platform(url: str) -> tuple[str | None, str | None]:
    """Devuelve (plataforma, identificador) o (None, None) si no se reconoce."""
    url = (url or "").strip()
    m = _TWITCH_RE.match(url)
    if m:
        return "twitch", m.group(1).lower()
    m = _KICK_RE.match(url)
    if m:
        return "kick", m.group(1).lower()
    m = _YT_RE.match(url)
    if m:
        return "youtube", m.group(1)
    m = _YT_CHANNEL_RE.match(url)
    if m:
        return "youtube", m.group(1)
    return None, None


def _http_json(url: str, headers: dict | None = None, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "VIRALCLIP-AI/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_twitch_live(channel: str, client_id: str, client_secret: str) -> dict:
    """Consulta real a Helix. Requiere client id + secret (app access token).
    Sin credenciales -> requires_credentials. Sin red -> not_verifiable."""
    if not client_id or not client_secret:
        return {"status": "requires_credentials",
                "detail": "Sin TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET no se puede consultar Helix."}
    try:
        token = _http_json(
            "https://id.twitch.tv/oauth2/token"
            f"?client_id={urllib.parse.quote(client_id)}"
            f"&client_secret={urllib.parse.quote(client_secret)}"
            "&grant_type=client_credentials",
        )
        data = _http_json(
            f"https://api.twitch.tv/helix/streams?user_login={urllib.parse.quote(channel)}",
            headers={"Client-ID": client_id, "Authorization": f"Bearer {token['access_token']}"},
        )
        streams = data.get("data", [])
        if streams:
            s = streams[0]
            return {"status": "live", "detail": f"En directo: {s.get('title','')} ({s.get('viewer_count',0)} espectadores)."}
        return {"status": "offline", "detail": "El canal no está en directo según Helix."}
    except Exception as e:  # red caída, credenciales inválidas, etc.
        return {"status": "not_verifiable", "detail": f"No se pudo consultar Helix: {e}"}


def check_kick_live(channel: str) -> dict:
    """Kick: API pública v2 sin clave. GET /api/v2/channels/<slug>.

    La v1 (/public/v1/channels) empezó a dar 401 el 2026-11-26, así
    que ya no sirve. La v2 devuelve el canal con `livestream` (objeto
    con session_title) cuando está en directo y None cuando no.
    Verificado 2026-11-26: ac7ionman offline -> livestream None, y la
    sonda streamlink coincidió (no está en directo).
    """
    try:
        data = _http_json(f"https://kick.com/api/v2/channels/{urllib.parse.quote(channel)}")
        ls = data.get("livestream")
        if ls:
            return {"status": "live", "detail": f"En directo: {ls.get('session_title','')}."}
        return {"status": "offline", "detail": "El canal no está en directo según la API de Kick."}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "not_found", "detail": "Canal no encontrado en Kick."}
        return {"status": "not_verifiable", "detail": f"No se pudo consultar la API de Kick: {e}"}
    except Exception as e:
        return {"status": "not_verifiable", "detail": f"No se pudo consultar la API de Kick: {e}"}


def check_youtube_video(video_id: str, api_key: str = "") -> dict:
    """Sin API key: oEmbed confirma que el vídeo existe, pero el estado en
    vivo NO es verificable. Con key: videos.list real."""
    if api_key:
        try:
            data = _http_json(
                "https://www.googleapis.com/youtube/v3/videos"
                f"?part=snippet,liveStreamingDetails&id={urllib.parse.quote(video_id)}&key={urllib.parse.quote(api_key)}"
            )
            items = data.get("items", [])
            if not items:
                return {"status": "not_found", "detail": "Vídeo no encontrado."}
            live = items[0].get("liveStreamingDetails", {})
            if live.get("actualStartTime") and not live.get("actualEndTime"):
                return {"status": "live", "detail": "Vídeo en directo (liveStreamingDetails)."}
            return {"status": "offline", "detail": "El vídeo no está en directo."}
        except Exception as e:
            return {"status": "not_verifiable", "detail": f"No se pudo consultar YouTube Data API: {e}"}
    try:
        _http_json(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}&format=json")
        return {"status": "requires_credentials",
                "detail": "El vídeo existe (oEmbed), pero el estado en vivo requiere YOUTUBE_API_KEY."}
    except Exception:
        return {"status": "not_found", "detail": "Vídeo no encontrado o no accesible (oEmbed)."}


def validate_stream_url(url: str, twitch_client_id: str = "", twitch_client_secret: str = "",
                        youtube_api_key: str = "") -> dict:
    """Valida sintaxis y, cuando es posible sin inventar, el estado en vivo."""
    platform, ident = detect_platform(url)
    if platform is None:
        return {"url": url, "platform": None, "identifier": None, "valid": False,
                "live_status": "invalid_url",
                "detail": "URL no reconocida: se esperan twitch.tv/<canal>, kick.com/<canal> o youtube.com/watch?v=... / youtu.be/..."}
    if platform == "twitch":
        live = check_twitch_live(ident, twitch_client_id, twitch_client_secret)
    elif platform == "kick":
        live = check_kick_live(ident)
    else:
        live = check_youtube_video(ident, youtube_api_key) if len(ident) > 6 and not ident.startswith("@") else {
            "status": "requires_credentials",
            "detail": "URL de canal de YouTube: el estado en vivo requiere YOUTUBE_API_KEY y el ID del vídeo en directo."}
    return {"url": url, "platform": platform, "identifier": ident, "valid": True,
            "live_status": live["status"], "detail": live["detail"]}
