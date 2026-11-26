"""Gestor de directos simultáneos: máximo 3 (por defecto 1 activo), cola de
espera, detener análisis. Estados: validating/live/waiting/processing/stopped/error."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

STATUSES = ("validating", "live", "waiting", "processing", "stopped", "error")


@dataclass
class StreamEntry:
    id: int
    platform: str
    channel: str
    url: str
    status: str = "validating"
    detail: str = ""


class LiveManager:
    def __init__(self, max_concurrent: int = 3, default_active: int = 1):
        self.max_concurrent = max_concurrent
        self.default_active = default_active
        self._lock = threading.Lock()
        self._streams: dict[int, StreamEntry] = {}
        self._next_id = 1

    def add(self, platform: str, channel: str, url: str) -> StreamEntry:
        with self._lock:
            active = sum(1 for s in self._streams.values() if s.status in ("live", "processing"))
            status = "live" if active < self.default_active else "waiting"
            entry = StreamEntry(self._next_id, platform, channel, url, status)
            self._streams[self._next_id] = entry
            self._next_id += 1
            return entry

    def set_status(self, stream_id: int, status: str, detail: str = "") -> StreamEntry | None:
        assert status in STATUSES, f"estado inválido: {status}"
        with self._lock:
            e = self._streams.get(stream_id)
            if e is None:
                return None
            e.status = status
            if detail:
                e.detail = detail
            if status in ("stopped", "error"):
                self._promote_waiting_locked()
            return e

    def _promote_waiting_locked(self) -> None:
        active = sum(1 for s in self._streams.values() if s.status in ("live", "processing"))
        if active >= self.max_concurrent:
            return
        for s in sorted(self._streams.values(), key=lambda x: x.id):
            if s.status == "waiting":
                s.status = "live"
                return

    def stop(self, stream_id: int) -> StreamEntry | None:
        return self.set_status(stream_id, "stopped", "Detenido por el usuario.")

    def get(self, stream_id: int) -> StreamEntry | None:
        return self._streams.get(stream_id)

    def list(self) -> list[StreamEntry]:
        return sorted(self._streams.values(), key=lambda x: x.id)

    def active_count(self) -> int:
        return sum(1 for s in self._streams.values() if s.status in ("live", "processing"))


live_manager = LiveManager()
