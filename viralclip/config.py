"""Configuración por variables de entorno. Sin secretos en el código."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "VIRALCLIP AI"
    environment: str = "development"
    database_url: str = "sqlite:///./viralclip.db"
    storage_backend: str = "local"  # local | s3
    storage_local_root: str = "./data/storage"
    # S3 / R2 (S3-compatible). Vacío = no configurado.
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = ""
    s3_region: str = "auto"
    # Estilo de direccionamiento S3: "auto" (virtual-hosted, R2) o "path"
    # (Supabase Storage exige path-style). Sin esto, boto3 falla contra
    # el endpoint S3 de Supabase. Ver docs/DESPLIEGUE.md §5.
    s3_addressing_style: str = "auto"
    # Plataformas (opcionales; sin ellas la validación en vivo es "requires_credentials")
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    youtube_api_key: str = ""
    # Whisper
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    # Descarga por enlace (YouTube, vídeos propios): duración máxima
    # de un vídeo descargado y clave del archivo de cookies en storage.
    max_download_duration_seconds: int = 7200  # 2 h
    youtube_cookies_key: str = "cookies/youtube_cookies.txt"
    youtube_cookies_meta_key: str = "cookies/youtube_meta.json"
    max_concurrent_streams: int = 3
    default_active_streams: int = 1
    clip_retention_days: int = 7
    storage_quota_gb: float = 10.0
    max_upload_gb: float = 2.0


settings = Settings()
