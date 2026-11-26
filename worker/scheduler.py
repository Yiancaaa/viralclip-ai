"""Scheduler: expiración de clips cada hora (APScheduler). Uso:
python -m worker.scheduler  (proceso aparte) o integrado en el worker."""
from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler

from viralclip.db import SessionLocal
from viralclip.storage import get_storage
from worker.expiry import expire_clips


def run_expiry():
    db = SessionLocal()
    try:
        result = expire_clips(db, get_storage())
        print(f"[expiry] {result}")
    finally:
        db.close()


if __name__ == "__main__":
    sched = BlockingScheduler()
    sched.add_job(run_expiry, "interval", hours=1)
    print("[scheduler] expiración de clips cada hora; Ctrl+C para parar")
    run_expiry()
    sched.start()
