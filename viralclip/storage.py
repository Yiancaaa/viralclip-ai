"""Almacenamiento abstraído: LocalStorage (desarrollo/pruebas) y S3Storage
(R2 / S3-compatible, por variables de entorno). Sin secretos en el código."""
from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class Storage(ABC):
    @abstractmethod
    def save(self, key: str, src_path: str) -> str:
        """Guarda el archivo local src_path bajo `key`; devuelve la clave/URI."""

    @abstractmethod
    def read(self, key: str) -> bytes:
        ...

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Borra el objeto; True si existía."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    def size_bytes(self, key: str) -> int:
        ...

    @abstractmethod
    def presigned_put_url(self, key: str, content_type: str = "video/mp4",
                          expires_in: int = 3600) -> str:
        """URL prefirmada para que un cliente suba DIRECTO al bucket con
        PUT, sin pasar por el proceso web. Lanza NotImplementedError si
        el backend no lo soporta (p. ej. almacenamiento local)."""


class LocalStorage(Storage):
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError("key fuera del root de almacenamiento")
        return p

    def save(self, key: str, src_path: str) -> str:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_path, dest)
        return key

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> bool:
        p = self._path(key)
        if p.exists():
            p.unlink()
            return True
        return False

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def size_bytes(self, key: str) -> int:
        p = self._path(key)
        return p.stat().st_size if p.exists() else 0

    def total_used_bytes(self) -> int:
        return sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())

    def presigned_put_url(self, key: str, content_type: str = "video/mp4",
                          expires_in: int = 3600) -> str:
        # El almacenamiento local no tiene URLs prefirmadas: la subida
        # directa solo existe con S3. La API responde 501 y el panel
        # usa la subida clásica multipart como reserva.
        raise NotImplementedError(
            "presigned_put_url no disponible en almacenamiento local; "
            "usa la subida clásica (multipart) o configura el backend S3")


class S3Storage(Storage):
    """R2 / S3-compatible vía boto3. Requiere S3_ENDPOINT_URL, credenciales y bucket."""

    def __init__(self, endpoint_url: str, access_key: str, secret_key: str, bucket: str, region: str = "auto",
                 addressing_style: str = "auto"):
        import boto3
        from botocore.config import Config

        if not all([endpoint_url, access_key, secret_key, bucket]):
            raise ValueError("S3Storage requiere endpoint, credenciales y bucket (variables de entorno)")
        self.bucket = bucket
        # Supabase Storage exige path-style; R2 funciona con virtual-hosted.
        client_kwargs: dict = {}
        if addressing_style == "path":
            client_kwargs["config"] = Config(
                s3={"addressing_style": "path"}, signature_version="s3v4"
            )
        self.client = boto3.client(
            "s3", endpoint_url=endpoint_url, aws_access_key_id=access_key,
            aws_secret_access_key=secret_key, region_name=region, **client_kwargs,
        )

    def save(self, key: str, src_path: str) -> str:
        self.client.upload_file(src_path, self.bucket, key)
        return f"s3://{self.bucket}/{key}"

    def read(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return False
        self.client.delete_object(Bucket=self.bucket, Key=key)
        return True

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def size_bytes(self, key: str) -> int:
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except Exception:
            return 0

    def presigned_put_url(self, key: str, content_type: str = "video/mp4",
                          expires_in: int = 3600) -> str:
        """URL prefirmada (SigV4) para un PUT directo al bucket.

        El navegador sube el archivo a esta URL con el MISMO
        Content-Type que se firma aquí; el proceso web no ve ni un
        byte del archivo (no hay lectura en RAM ni proxy de Render de
        por medio). Válida `expires_in` segundos.
        """
        return self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key,
                    "ContentType": content_type},
            ExpiresIn=int(expires_in),
        )


def get_storage() -> Storage:
    from .config import settings

    if settings.storage_backend == "s3":
        return S3Storage(
            settings.s3_endpoint_url, settings.s3_access_key_id,
            settings.s3_secret_access_key, settings.s3_bucket, settings.s3_region,
            settings.s3_addressing_style,
        )
    return LocalStorage(settings.storage_local_root)


def used_bytes_local(root: str | Path) -> int:
    root = Path(root)
    if not root.exists():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
