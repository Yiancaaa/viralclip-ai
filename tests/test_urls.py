"""Validación de URLs: plataformas, válidas e inválidas. Sin red externa
(salvo los casos simulados con mock)."""
from unittest.mock import patch

from viralclip.connectors import detect_platform, validate_stream_url


def test_detect_twitch():
    assert detect_platform("https://www.twitch.tv/ibai") == ("twitch", "ibai")
    assert detect_platform("twitch.tv/xqc") == ("twitch", "xqc")


def test_detect_kick():
    assert detect_platform("https://kick.com/westcol") == ("kick", "westcol")


def test_detect_youtube():
    assert detect_platform("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == ("youtube", "dQw4w9WgXcQ")
    assert detect_platform("https://youtu.be/dQw4w9WgXcQ") == ("youtube", "dQw4w9WgXcQ")
    assert detect_platform("https://www.youtube.com/live/dQw4w9WgXcQ") == ("youtube", "dQw4w9WgXcQ")


def test_detect_invalid():
    assert detect_platform("https://example.com/video") == (None, None)
    assert detect_platform("not a url") == (None, None)
    assert detect_platform("") == (None, None)


def test_validate_invalid_url():
    r = validate_stream_url("https://example.com/x")
    assert r["valid"] is False and r["platform"] is None


def test_validate_twitch_without_credentials_is_honest():
    # Sin credenciales NO se inventa el estado en vivo.
    r = validate_stream_url("https://twitch.tv/ibai", "", "")
    assert r["valid"] is True and r["platform"] == "twitch"
    assert r["live_status"] == "requires_credentials"


def test_validate_kick_live_mocked():
    with patch("viralclip.connectors._http_json",
               return_value={"data": [{"slug": "westcol", "livestream": {"session_title": "Directo"}}]}):
        r = validate_stream_url("https://kick.com/westcol")
    assert r["live_status"] == "live"


def test_validate_kick_offline_mocked():
    with patch("viralclip.connectors._http_json",
               return_value={"data": [{"slug": "westcol", "livestream": None}]}):
        r = validate_stream_url("https://kick.com/westcol")
    assert r["live_status"] == "offline"
