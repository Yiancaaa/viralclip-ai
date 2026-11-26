"""Captura HLS de directos en vivo (Twitch / Kick / YouTube Live) con
streamlink + ffmpeg, y ciclo de directo (capturar -> procesar -> clips).

Reglas de honestidad: los errores son distintos y explícitos — un canal
sin directo NO es lo mismo que un bloqueo de la plataforma ni que un
fallo de red. Ninguna función inventa que algo está en vivo: si no se
puede comprobar, lo dice (`no_verificable`).

Compatibilidad streamlink/urllib3 (verificado 2026-11-26):
streamlink 7.2.0 (el de `requirements.txt`) parchea
``urllib3.util.url.PERCENT_RE`` con su clase
``Urllib3UtilUrlPercentReOverride``, que solo define ``subn``. Desde
urllib3>=2.5, `urllib3/util/url.py` llama también a ``_PERCENT_RE.sub``
(normalización de zona IPv6 y de host), así que CUALQUIER petición de
streamlink muere con
``AttributeError: ... has no attribute 'sub'``. La prueba E2E del MVP
usó streamlink 8.6.3 en otro venv, por eso funcionó entonces y el
módulo de la app no. Aquí se aplica un shim defensivo al importar el
módulo (solo si falta `sub`; no toca nada si un streamlink futuro ya
lo trae). El arreglo correcto a largo plazo es subir streamlink en
`requirements.txt` o fijar urllib3<2.5; el shim solo mantiene el
código de la app funcionando con lo instalado.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- shim streamlink
_SHIM_APPLIED = False


def apply_streamlink_shim() -> bool:
    """Aplica el shim de compatibilidad streamlink 7.2.0 / urllib3>=2.5.

    Devuelve True si el shim se aplicó (o ya estaba), False si no hizo
    falta o si streamlink no está instalado. Idempotente y defensivo:
    nunca rompe la importación de este módulo.
    """
    global _SHIM_APPLIED
    if _SHIM_APPLIED:
        return True
    try:
        from streamlink.session.http import Urllib3UtilUrlPercentReOverride
    except Exception:
        return False
    if not hasattr(Urllib3UtilUrlPercentReOverride, "sub"):
        @classmethod
        def _sub(cls, repl, string, count=0):  # noqa: ARG001 - firma de re.sub
            # Igual que `subn` del override: no tocar el texto (no
            # mayusculizar los %XX), solo devolverlo tal cual.
            return string

        Urllib3UtilUrlPercentReOverride.sub = _sub
    _SHIM_APPLIED = True
    return True


apply_streamlink_shim()


# ---------------------------------------------------------------- errores distintos
class HLSError(Exception):
    """Error de captura HLS con código distinto y mensaje para el usuario.

    Códigos:
    - not_live: el canal existe pero no está en directo ahora.
    - not_found: el canal/vídeo no existe.
    - no_access: la plataforma exige inicio de sesión o suscripción
      (403, "login required", contenido solo para suscriptores).
    - blocked: la plataforma bloquea el acceso desde este servidor
      (bot-check, "Sign in to confirm you're not a bot", IP marcada).
    - timeout: no hubo respuesta a tiempo (red lenta o servidor caído).
    - no_verifiable: no se pudo comprobar el estado (sin red, sin
      credenciales para la API oficial, error desconocido de red).
    - missing_dependency: falta streamlink o ffmpeg.
    - failed: cualquier otro fallo de captura.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def classify_streamlink_error(text: str) -> tuple[str, str]:
    """Clasifica un mensaje de error de streamlink en (código, mensaje)."""
    low = (text or "").lower()
    if any(p in low for p in ("sign in to confirm you're not a bot",
                              "not a bot", "confirm you're not a bot",
                              "bot check", "captcha", "unusual traffic")):
        return "blocked", ("La plataforma bloquea el acceso desde este "
                           "servidor (verificación anti-bot).")
    if any(p in low for p in ("could not get video info", "unplayable")):
        return "blocked", ("La plataforma no devolvió la información del "
                           "directo (bloqueo anti-bot o restricción); no se "
                           "pudo comprobar si está en vivo.")
    if any(p in low for p in ("login required", "log in", "sign in to",
                              "subscription", "subscriber-only",
                              "this channel is not available")):
        return "no_access", ("El directo exige inicio de sesión o "
                             "suscripción en la plataforma.")
    if "403" in low or "forbidden" in low:
        return "no_access", ("La plataforma devolvió 403 (acceso denegado) "
                             "para este directo.")
    if "404" in low or "not found" in low:
        return "not_found", "Canal o vídeo no encontrado en la plataforma."
    if any(p in low for p in ("no playable streams", "no streams found",
                              "offline", "not live", "no longer live",
                              "stream is offline")):
        return "not_live", "El canal no está en directo ahora mismo."
    if any(p in low for p in ("timed out", "timeout", "timedout",
                              "connection reset", "connection aborted")):
        return "timeout", "Tiempo de espera agotado al contactar la plataforma."
    return "failed", f"Captura fallida: {text[:300]}"


# ---------------------------------------------------------------- sonda de directo
def _get_streamlink_session():
    apply_streamlink_shim()
    from streamlink import Streamlink
    return Streamlink()


def _resolve_streams(session, url: str) -> dict:
    """Resuelve los streams de una URL capturando los logs de streamlink.

    Algunos plugins (YouTube) NO lanzan excepción cuando no pueden
    resolver: registran "Could not get video info - UNPLAYABLE" y
    devuelven {}. Sin esto, un directo bloqueado se confundiría con un
    canal offline. Si hay mensajes de ese tipo, se lanza HLSError con
    el código que corresponda en vez de devolver {}.
    """
    import logging

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("streamlink")
    handler = _Capture()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        streams = session.streams(url)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    if not streams:
        joined = " ".join(records)
        if any(p in joined for p in ("UNPLAYABLE", "Could not get video info",
                                     "403", "Forbidden")):
            code, msg = classify_streamlink_error(joined)
            raise HLSError(code, msg)
    return streams or {}


def probe_live(url: str, timeout: int = 30) -> dict:
    """Comprueba si una URL de directo está en vivo con streamlink.

    Devuelve {"live": bool, "streams": [calidades], "best": str|None,
    "detail": str}. Nunca inventa el estado: si streamlink no puede
    resolver, lanza HLSError con el código que corresponda.
    """
    if shutil.which("ffmpeg") is None:
        raise HLSError("missing_dependency", "ffmpeg no disponible")
    try:
        session = _get_streamlink_session()
    except Exception as e:
        raise HLSError("missing_dependency", f"streamlink no disponible: {e}")
    try:
        streams = _resolve_streams(session, url)
    except HLSError:
        raise
    except Exception as e:
        code, msg = classify_streamlink_error(str(e))
        raise HLSError(code, msg) from e
    if not streams:
        return {"live": False, "streams": [], "best": None,
                "detail": "Sin streams reproducibles: el canal no está en directo."}
    names = sorted(streams.keys())
    best = "best" if "best" in streams else names[-1]
    return {"live": True, "streams": names, "best": best,
            "detail": f"En directo ({len(names)} calidades: {', '.join(names[:4])})."}


def resolve_hls_url(url: str, quality: str = "best", timeout: int = 30) -> str:
    """Resuelve la URL HLS (playlist .m3u8) de un directo en vivo.

    Lanza HLSError("not_live") si no hay streams, o el código que
    corresponda si la plataforma deniega el acceso.
    """
    try:
        session = _get_streamlink_session()
        streams = _resolve_streams(session, url)
    except HLSError:
        raise
    except Exception as e:
        code, msg = classify_streamlink_error(str(e))
        raise HLSError(code, msg) from e
    if not streams:
        raise HLSError("not_live", "El canal no está en directo ahora mismo.")
    stream = streams.get(quality) or streams.get("best")
    if stream is None:
        stream = next(iter(streams.values()))
    hls_url = getattr(stream, "url", None)
    if not hls_url:
        raise HLSError("failed", "streamlink resolvió el directo pero no dio URL HLS.")
    return hls_url


# ---------------------------------------------------------------- captura
def _ffprobe_info(path: str) -> dict:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_type,codec_name,width,height",
         "-of", "json", path], text=True, timeout=60)
    import json
    return json.loads(out)


def capture_segment(url: str, duration: float, out_path: str,
                    quality: str = "best", retries: int = 1,
                    timeout: int | None = None) -> str:
    """Captura `duration` segundos de un directo en vivo a MP4.

    Mecanismo (el probado en el MVP, 2026-11-26): streamlink descarga
    el directo y ffmpeg lo re-codifica a H.264 + AAC desde el pipe.
    Todo ocurre EN EL PROCESO (librería streamlink + un hilo que
    bombea bytes al stdin de ffmpeg): NO se usa el CLI de streamlink
    ni se pasa la URL HLS directa a ffmpeg. Dos motivos medidos
    2026-11-26 en esta VM:
    - ffmpeg no puede abrir la playlist HLS directa (``Error opening
      input: Input/output error`` con peereira7 en Twitch): detrás
      del proxy que intercepta TLS, ffmpeg no descarga la playlist,
      mientras que el stack HTTP de streamlink sí descarga los
      segmentos.
    - el CLI de streamlink 7.2.0 está roto con urllib3>=2.5 (el mismo
      bug del shim: `AttributeError: ... has no attribute 'sub'`); el
      shim de este módulo solo vive en nuestro proceso, así que un
      subproceso CLI no lo tendría. La librería, con el shim aplicado,
      resuelve y descarga bien.
    `pipeline.capture_segment` usa el CLI por pipe; este es su
    equivalente robusto en proceso (con sonda previa, reintentos y
    errores distintos).

    Dieta de memoria (512 MB en Render Free): ffmpeg a 1 hilo
    (`-threads 1`); la transcripción posterior corre en subproceso
    (ver `worker/jobs.py` -> `transcribe_subprocess`), nunca en el
    proceso web.

    Reintentos solo para fallos transitorios (timeout / red / ffmpeg
    sin archivo); `not_live`, `no_access` y `blocked` NO se reintentan.
    Lanza HLSError con código distinto. Verifica el archivo con ffprobe
    (duración > 0 y al menos un stream de vídeo).
    """
    if shutil.which("ffmpeg") is None:
        raise HLSError("missing_dependency", "ffmpeg no disponible")
    if duration <= 0:
        raise ValueError("duration debe ser > 0")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if timeout is None:
        timeout = int(duration) + 180
    last: HLSError | None = None
    for attempt in range(retries + 1):
        # Sonda previa: si no está en vivo, no se lanza nada.
        probe = probe_live(url)
        if not probe["live"]:
            raise HLSError("not_live", probe["detail"])
        try:
            stream = _open_stream(url, quality)
        except HLSError as e:
            if e.code in ("not_live", "no_access", "blocked"):
                raise
            last = e
            if attempt < retries:
                time.sleep(5)
                continue
            raise last
        try:
            ff = subprocess.Popen(
                ["ffmpeg", "-y", "-v", "error", "-threads", "1",
                 "-i", "pipe:0", "-t", str(duration),
                 "-c:v", "libx264", "-preset", "veryfast",
                 "-c:a", "aac", out_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        except Exception as e:
            last = HLSError("failed", f"no se pudo lanzar ffmpeg: {e}")
            try:
                stream.close()
            except Exception:
                pass
            if attempt < retries:
                time.sleep(5)
                continue
            raise last from e
        pump_error: list[Exception] = []

        def _pump() -> None:
            try:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    try:
                        ff.stdin.write(chunk)
                    except BrokenPipeError:
                        # ffmpeg cerró stdin al cumplir -t (salida normal):
                        # no es un fallo de streamlink, se termina el bombeo.
                        break
            except Exception as e:  # noqa: BLE001 - el error se reporta fuera
                pump_error.append(e)
            finally:
                try:
                    ff.stdin.close()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass

        pump = threading.Thread(target=_pump, daemon=True)
        pump.start()
        # NUNCA ff.communicate() aquí: cierra stdin mientras el hilo
        # de bombeo escribe y rompe la captura (ffmpeg recibe EOF y
        # falla con "Invalid data found"). Se espera con wait() y se
        # lee stderr en un hilo aparte.
        stderr_chunks: list[bytes] = []

        def _read_stderr() -> None:
            try:
                while True:
                    data = ff.stderr.read(4096)
                    if not data:
                        break
                    stderr_chunks.append(data)
            except Exception:
                pass

        err_thread = threading.Thread(target=_read_stderr, daemon=True)
        err_thread.start()
        try:
            ff.wait(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            last = HLSError("timeout", f"ffmpeg no terminó en {timeout} s")
            try:
                ff.kill()
            except Exception:
                pass
            pump.join(timeout=10)
            err_thread.join(timeout=5)
            if attempt < retries:
                time.sleep(5)
                continue
            raise last from e
        pump.join(timeout=10)
        err_thread.join(timeout=5)
        stderr = b"".join(stderr_chunks)
        if ff.returncode != 0 or not os.path.exists(out_path):
            if pump_error:
                err = pump_error[0]
                code, msg = classify_streamlink_error(str(err))
                if code in ("not_live", "no_access", "blocked"):
                    raise HLSError(code, msg) from err
                last = HLSError("failed", f"streamlink dejó de servir datos: {err}")
                if attempt < retries:
                    time.sleep(5)
                    continue
                raise last from err
            err = _decode(stderr)[-300:]
            code, msg = classify_streamlink_error(err)
            if code in ("not_live", "no_access", "blocked"):
                raise HLSError(code, msg)
            last = HLSError("failed", f"ffmpeg falló: {err}")
            if attempt < retries:
                time.sleep(5)
                continue
            raise last
        # Verificación con ffprobe: el archivo debe existir, durar y
        # tener vídeo (un .mp4 de 0 s o sin vídeo no sirve para procesar).
        try:
            info = _ffprobe_info(out_path)
        except Exception as e:
            last = HLSError("failed", f"ffprobe no pudo leer la captura: {e}")
            if attempt < retries:
                time.sleep(5)
                continue
            raise last from e
        dur = float(info.get("format", {}).get("duration") or 0)
        kinds = {s.get("codec_type") for s in info.get("streams", [])}
        if dur <= 0 or "video" not in kinds:
            last = HLSError("failed",
                            f"captura vacía o sin vídeo (duración {dur} s, streams {sorted(kinds)})")
            if attempt < retries:
                time.sleep(5)
                continue
            raise last
        return out_path
    assert last is not None
    raise last


def _open_stream(url: str, quality: str = "best"):
    """Abre el stream de streamlink (objeto legible) para una URL en
    vivo. Lanza HLSError con el código que corresponda."""
    try:
        session = _get_streamlink_session()
        streams = _resolve_streams(session, url)
    except HLSError:
        raise
    except Exception as e:
        code, msg = classify_streamlink_error(str(e))
        raise HLSError(code, msg) from e
    if not streams:
        raise HLSError("not_live", "El canal no está en directo ahora mismo.")
    stream = streams.get(quality) or streams.get("best")
    if stream is None:
        stream = next(iter(streams.values()))
    try:
        return stream.open()
    except Exception as e:
        code, msg = classify_streamlink_error(str(e))
        raise HLSError(code, msg) from e


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------- ciclo de directo
@dataclass
class LiveCycleResult:
    stream_id: int
    platform: str
    channel: str
    video_id: int | None = None
    job_id: int | None = None
    status: str = "done"
    detail: str = ""
    error_code: str | None = None


def run_live_cycle(db, storage, entry, segment_seconds: float = 120.0,
                   workdir: str = "./data/work") -> LiveCycleResult:
    """Un ciclo de directo: sonda -> captura un segmento -> crea el
    Video -> lanza process_job (que transcribe en subproceso, detecta
    momentos, puntúa, titula y renderiza clips 1080x1920 en la
    biblioteca).

    `entry` es un StreamEntry de worker.live (id, platform, channel,
    url). `db` es una sesión SQLAlchemy y `storage` el almacenamiento
    configurado. Marca el estado en live_manager ("processing" durante
    el ciclo; "live" al terminar si el directo sigue; "error" con el
    código si la sonda o la captura fallan).

    No inventa momentos: si el segmento no tiene audio/transcripción,
    process_job no genera clips y el ciclo termina en "done" sin clips.
    """
    from viralclip.models import Job, Video
    from worker.jobs import process_job
    from worker.live import live_manager

    result = LiveCycleResult(stream_id=entry.id, platform=entry.platform,
                             channel=entry.channel)
    live_manager.set_status(entry.id, "processing")
    try:
        probe = probe_live(entry.url)
        if not probe["live"]:
            raise HLSError("not_live", probe["detail"])
        seg_path = os.path.join(workdir, f"live_{entry.id}_{int(time.time())}.mp4")
        capture_segment(entry.url, segment_seconds, seg_path)
        # Guardar el segmento como Video (original temporal: process_job
        # lo lee y los clips quedan en la biblioteca; el original NO es
        # permanente, se borra con la caducidad de 7 días del clip).
        import hashlib
        data = Path(seg_path).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        key = f"originals/live_{entry.id}_{sha[:12]}.mp4"
        storage.save(key, seg_path)
        try:
            os.remove(seg_path)
        except OSError:
            pass
        video = Video(title=f"{entry.channel} en directo ({entry.platform})",
                      platform=entry.platform, source_url=entry.url,
                      duration_seconds=float(segment_seconds),
                      file_path=key, file_size_bytes=len(data), sha256=sha)
        db.add(video)
        db.commit()
        db.refresh(video)
        result.video_id = video.id
        job = Job(video_id=video.id, kind="process", status="queued")
        db.add(job)
        db.commit()
        db.refresh(job)
        result.job_id = job.id
        process_job(db, job.id, storage, workdir=workdir)
        result.status = "done"
        result.detail = (f"Segmento de {segment_seconds:.0f} s procesado "
                         f"(video {video.id}, job {job.id}).")
        live_manager.set_status(entry.id, "live", result.detail)
        return result
    except HLSError as e:
        result.status = "error"
        result.error_code = e.code
        result.detail = e.message
        live_manager.set_status(entry.id, "error", e.message)
        return result
    except Exception as e:  # noqa: BLE001 - el ciclo no debe tumbar el hilo
        result.status = "error"
        result.error_code = "failed"
        result.detail = str(e)[:500]
        live_manager.set_status(entry.id, "error", result.detail)
        return result


class LiveSessionRunner:
    """Bucle de directo: repite ciclos de captura+proceso mientras el
    directo siga en vivo y no se pida parar.

    Respeta el máximo de 3 directos simultáneos: un semáforo de clase
    (3 plazas) se adquiere al arrancar; si no hay plaza, el directo
    queda en "waiting" en live_manager y `start()` devuelve False (la
    cola lo promociona cuando se libera una plaza, ver
    `LiveManager._promote_waiting_locked`).

    Uso previsto (lo cablea el coordinador en la API, este módulo no
    toca `api/main.py`): al arrancar un directo (`POST
    /api/streams/{id}/start`) lanzar `runner.start(entry)` en un hilo
    daemon; al parar (`POST /api/streams/{id}/stop`) llamar
    `runner.stop(stream_id)`.
    """

    MAX_CONCURRENT = 3

    def __init__(self, db_factory, storage, segment_seconds: float = 120.0,
                 workdir: str = "./data/work"):
        self._db_factory = db_factory
        self._storage = storage
        self.segment_seconds = segment_seconds
        self.workdir = workdir
        self._sem = threading.BoundedSemaphore(self.MAX_CONCURRENT)
        self._threads: dict[int, threading.Thread] = {}
        self._stop_events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()

    def start(self, entry) -> bool:
        """Arranca el bucle en un hilo. False si no hay plaza libre
        (el directo queda en espera) o si ya está corriendo."""
        from worker.live import live_manager

        with self._lock:
            if entry.id in self._threads and self._threads[entry.id].is_alive():
                return True
            if not self._sem.acquire(blocking=False):
                return False
            stop = threading.Event()
            self._stop_events[entry.id] = stop
            t = threading.Thread(target=self._loop, args=(entry, stop),
                                 daemon=True, name=f"live-{entry.id}")
            self._threads[entry.id] = t
            live_manager.set_status(entry.id, "live")
            t.start()
            return True

    def stop(self, stream_id: int) -> None:
        from worker.live import live_manager

        with self._lock:
            ev = self._stop_events.get(stream_id)
            if ev:
                ev.set()
            live_manager.stop(stream_id)

    def _loop(self, entry, stop: threading.Event) -> None:
        from worker.live import live_manager

        try:
            while not stop.is_set():
                current = live_manager.get(entry.id)
                if current is None or current.status in ("stopped", "error"):
                    break
                db = self._db_factory()
                try:
                    res = run_live_cycle(db, self._storage, entry,
                                         segment_seconds=self.segment_seconds,
                                         workdir=self.workdir)
                finally:
                    db.close()
                if res.status == "error":
                    # not_live / no_access / blocked: no tiene sentido
                    # reintentar el mismo directo en bucle; el estado
                    # ya quedó en "error" con el código.
                    break
                if stop.wait(timeout=5):
                    break
        finally:
            self._sem.release()
            with self._lock:
                self._threads.pop(entry.id, None)
                self._stop_events.pop(entry.id, None)
