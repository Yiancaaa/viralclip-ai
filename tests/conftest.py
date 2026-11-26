import os
import tempfile

TMP = tempfile.mkdtemp(prefix="viralclip_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["STORAGE_LOCAL_ROOT"] = f"{TMP}/storage"
os.environ["TWITCH_CLIENT_ID"] = ""
os.environ["TWITCH_CLIENT_SECRET"] = ""
os.environ["YOUTUBE_API_KEY"] = ""

import pytest  # noqa: E402
from viralclip.db import Base, SessionLocal, engine, init_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db():
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
