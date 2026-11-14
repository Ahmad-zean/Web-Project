import importlib
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["APP_ENV"] = "test"
os.environ["JWT_SECRET_KEY"] = "test-only-signing-key-with-at-least-thirty-two-characters"
os.environ["TRUSTED_HOSTS"] = "testserver"
os.environ["DATABASE_URL"] = "sqlite:///./test-only-unused.db"

import main
from database import Base


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        with test_session() as db:
            yield db

    monkeypatch.setattr(main, "SessionLocal", test_session)
    monkeypatch.setattr(main, "PROFILE_PICS_DIR", tmp_path / "profile_pics")
    main.PROFILE_PICS_DIR.mkdir(parents=True, exist_ok=True)
    main.app.dependency_overrides[main.get_db] = override_get_db

    sent_verification_emails = []
    sent_reset_emails = []
    monkeypatch.setattr(
        main,
        "send_verification_email",
        lambda email, token: sent_verification_emails.append((email, token)),
    )
    monkeypatch.setattr(
        main,
        "send_password_reset_email",
        lambda email, token: sent_reset_emails.append((email, token)),
    )
    user_router = importlib.import_module("Routers.User")
    monkeypatch.setattr(
        user_router,
        "send_verification_email",
        lambda email, token: sent_verification_emails.append((email, token)),
    )

    try:
        with TestClient(main.app, base_url="http://testserver") as client:
            yield client, test_session, sent_verification_emails, sent_reset_emails
    finally:
        main.app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()
