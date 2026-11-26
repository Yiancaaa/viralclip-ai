"""Descarga por enlace (yt-dlp) con el extractor simulado: sin red."""
from pathlib import Path

import pytest

from worker.download import (DownloadError, download_video_url, extract_info,
                             is_youtube_video_url)

URL = "https://www.youtube.com/watch?v=Dxi1CRQMQZo"
INFO = {"id": "Dxi1CRQMQZo", "title": "La casa que solo existe de noche",
        "duration": 107, "uploader": "Yiancaaa", "filesize_approx": 9_000_000}


class FakeYDL:
    """Simula yt_dlp.YoutubeDL. Configurable por clase en cada test."""
    info = dict(INFO)
    raise_on_info = None
    raise_on_download = None
    seen_opts = None

    def __init__(self, opts):
        FakeYDL.seen_opts = opts
        self._outtmpl = opts.get("outtmpl")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if FakeYDL.raise_on_info:
            raise FakeYDL.raise_on_info
        return dict(FakeYDL.info)

    def download(self, urls):
        if FakeYDL.raise_on_download:
            raise FakeYDL.raise_on_download
        # Imita la salida real: %(id)s.%(ext)s en el outtmpl.
        dest = Path(self._outtmpl.replace("%(id)s", FakeYDL.info["id"])
                    .replace("%(ext)s", "mp4"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"FAKEVIDEO" * 100)


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    FakeYDL.info = dict(INFO)
    FakeYDL.raise_on_info = None
    FakeYDL.raise_on_download = None
    FakeYDL.seen_opts = None
    yield


def test_is_youtube_video_url():
    assert is_youtube_video_url(URL) == (True, "Dxi1CRQMQZo")
    assert is_youtube_video_url("https://youtu.be/Dxi1CRQMQZo")[0] is True
    assert is_youtube_video_url("https://www.youtube.com/shorts/Dxi1CRQMQZo")[0] is True
    assert is_youtube_video_url("https://www.youtube.com/@Yiancaaa") == (False, None)
    assert is_youtube_video_url("https://twitch.tv/ibai") == (False, None)
    assert is_youtube_video_url("no es una url") == (False, None)


def test_extract_info_ok_and_cookies_passthrough(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\txyz\n")
    info = extract_info(URL, cookies_path=str(cookies))
    assert info["title"] == "La casa que solo existe de noche"
    assert info["duration_seconds"] == 107
    assert FakeYDL.seen_opts["cookiefile"] == str(cookies)
    assert FakeYDL.seen_opts["noplaylist"] is True


def test_extract_info_blocked_maps_to_blocked():
    FakeYDL.raise_on_info = Exception(
        "Sign in to confirm you're not a bot")
    with pytest.raises(DownloadError) as ei:
        extract_info(URL)
    assert ei.value.code == "blocked"
    assert "cookies" in ei.value.message


def test_extract_info_429_maps_to_blocked():
    FakeYDL.raise_on_info = Exception("HTTP Error 429: Too Many Requests")
    with pytest.raises(DownloadError) as ei:
        extract_info(URL)
    assert ei.value.code == "blocked"


def test_extract_info_unavailable():
    FakeYDL.raise_on_info = Exception("Video unavailable")
    with pytest.raises(DownloadError) as ei:
        extract_info(URL)
    assert ei.value.code == "unavailable"


def test_extract_info_ssl_error_is_failed_not_unavailable():
    # Regresión: «page» contiene «age»; el chequeo de restricción de
    # edad no debe dispararse con cualquier mensaje que tenga «page».
    FakeYDL.raise_on_info = Exception(
        "Unable to download API page: [SSL: CERTIFICATE_VERIFY_FAILED]")
    with pytest.raises(DownloadError) as ei:
        extract_info(URL)
    assert ei.value.code == "failed"


def test_extract_info_age_gate_is_unavailable():
    FakeYDL.raise_on_info = Exception("Sign in to confirm your age")
    with pytest.raises(DownloadError) as ei:
        extract_info(URL)
    assert ei.value.code == "unavailable"


def test_extract_info_rejects_non_youtube():
    with pytest.raises(DownloadError):
        extract_info("https://twitch.tv/ibai")


def test_download_ok(tmp_path):
    out = download_video_url(URL, tmp_path)
    assert Path(out["path"]).exists()
    assert out["size_bytes"] == 900
    assert out["title"].startswith("La casa")
    assert out["duration_seconds"] == 107


def test_download_too_long(tmp_path):
    FakeYDL.info = dict(INFO, duration=8000)
    with pytest.raises(DownloadError) as ei:
        download_video_url(URL, tmp_path, max_duration_s=7200)
    assert ei.value.code == "too_long"


def test_download_too_big_by_info(tmp_path):
    FakeYDL.info = dict(INFO, filesize_approx=3 * 1024 ** 3)
    with pytest.raises(DownloadError) as ei:
        download_video_url(URL, tmp_path, max_size_bytes=2 * 1024 ** 3)
    assert ei.value.code == "too_big"


def test_download_error_maps_to_blocked(tmp_path):
    FakeYDL.raise_on_download = Exception("Sign in to confirm you're not a bot")
    with pytest.raises(DownloadError) as ei:
        download_video_url(URL, tmp_path)
    assert ei.value.code == "blocked"
