"""Base de datos: SQLAlchemy. SQLite para desarrollo; DATABASE_URL lista para Postgres."""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (registra las tablas)

    Base.metadata.create_all(bind=engine)
    # Migración ligera para BD SQLite ya creadas: create_all no añade
    # columnas a tablas existentes; se añaden si faltan (idempotente).
    from sqlalchemy import text

    _new_cols = {
        "category": "VARCHAR(50) DEFAULT ''",
        "subcategory": "VARCHAR(50) DEFAULT ''",
        "classification_confidence": "FLOAT DEFAULT 0.0",
        "classification_source": "VARCHAR(20) DEFAULT ''",
        "manual_override": "BOOLEAN DEFAULT 0",
        "classification_signals": "TEXT DEFAULT '{}'",
    }
    try:
        with engine.begin() as conn:
            existing = {r[1] for r in conn.execute(text("PRAGMA table_info(videos)"))}
            for col, ddl in _new_cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE videos ADD COLUMN {col} {ddl}"))
    except Exception:
        pass  # en Postgres la migración la hace el despliegue, no el arranque
