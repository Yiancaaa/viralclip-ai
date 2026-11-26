"""Modelos: videos, jobs, moments, clips, live_sessions, favorites, quotas."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from .db import Base

RETENTION_DAYS = 7


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_expires() -> datetime:
    return utcnow() + timedelta(days=RETENTION_DAYS)


class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False, default="")
    source_url = Column(String(1000), default="")
    platform = Column(String(50), default="upload")
    duration_seconds = Column(Float, default=0.0)
    file_path = Column(String(1000), default="")
    file_size_bytes = Column(Integer, default=0)
    sha256 = Column(String(64), default="")
    created_at = Column(DateTime(timezone=True), default=utcnow)
    # Clasificación del tipo de directo (worker/classify.py):
    # category en {gameplay_streamer, podcast_entrevista, reality_casa,
    # conversacion_reaccion, otros}; subcategory interna; source 'auto' o
    # 'manual'. manual_override=True => la corrección del usuario tiene
    # prioridad sobre la automática y no se sobrescribe al reclasificar.
    category = Column(String(50), default="")
    subcategory = Column(String(50), default="")
    classification_confidence = Column(Float, default=0.0)
    classification_source = Column(String(20), default="")
    manual_override = Column(Boolean, default=False)
    classification_signals = Column(Text, default="{}")
    jobs = relationship("Job", back_populates="video", cascade="all, delete-orphan")
    clips = relationship("Clip", back_populates="video", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=True)
    kind = Column(String(50), default="process")  # process | live
    status = Column(String(50), default="queued")  # queued/processing/done/error/cancelled
    error = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    video = relationship("Video", back_populates="jobs")
    moments = relationship("Moment", back_populates="job", cascade="all, delete-orphan")


class Moment(Base):
    __tablename__ = "moments"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=True)
    start_seconds = Column(Float, nullable=False)
    end_seconds = Column(Float, nullable=False)
    score = Column(Float, default=0.0)
    score_breakdown = Column(Text, default="{}")  # JSON
    transcript_text = Column(Text, default="")
    titles = Column(Text, default="[]")  # JSON list
    created_at = Column(DateTime(timezone=True), default=utcnow)
    job = relationship("Job", back_populates="moments")


class Clip(Base):
    __tablename__ = "clips"
    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=True)
    moment_id = Column(Integer, ForeignKey("moments.id"), nullable=True)
    title = Column(String(500), default="")
    platform = Column(String(50), default="")
    duration_seconds = Column(Float, default=0.0)
    score = Column(Float, default=0.0)
    file_path = Column(String(1000), default="")
    thumbnail_path = Column(String(1000), default="")
    file_size_bytes = Column(Integer, default=0)
    favorite = Column(Boolean, default=False)
    expired = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    expires_at = Column(DateTime(timezone=True), default=default_expires)
    video = relationship("Video", back_populates="clips")


class LiveSession(Base):
    __tablename__ = "live_sessions"
    id = Column(Integer, primary_key=True)
    platform = Column(String(50), nullable=False)
    channel = Column(String(200), nullable=False)
    url = Column(String(1000), default="")
    status = Column(String(50), default="validating")
    # validating | live | waiting | processing | stopped | error
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    detail = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=utcnow)


class Quota(Base):
    __tablename__ = "quotas"
    id = Column(Integer, primary_key=True)
    scope = Column(String(100), unique=True, default="default")
    limit_gb = Column(Float, default=10.0)
    used_bytes = Column(Integer, default=0)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
