from datetime import UTC, datetime, timedelta
from io import BytesIO

from PIL import Image
from fastapi.testclient import TestClient
from sqlalchemy import select

import main
import models
from config import settings
from models import Post
from security import CSRF_COOKIE_NAME
from services import (
    ACCESS_TOKEN_COOKIE,
    authenticate_user,
    create_access_token,
    create_email_action_token,
    hash_password,
    verify_password,
)


def create_verified_user(db, username: str, email: str) -> models.User:
    user = models.User(
        username=username,
        email=email,
        password_hash="",
        email_verified_at=datetime.now(UTC),
        auth_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_client(client: TestClient, user: models.User) -> None:
    client.cookies.set(
        ACCESS_TOKEN_COOKIE,
        create_access_token(user.id, user.auth_version),
    )


def csrf_token(client: TestClient) -> str:
    client.get("/")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return token


def test_login_form_requires_csrf_token(app_client):
    client, _, _, _ = app_client

    response = client.post(
        "/login",
        data={"identifier": "nobody@example.test", "password": "not-a-password"},
    )

    assert response.status_code == 403


def test_registration_verification_then_login(app_client):
    client, session_factory, sent_verification_emails, _ = app_client
    token = csrf_token(client)

    response = client.post(
        "/register",
        data={
            "csrf_token": token,
            "username": "verified_user",
            "email": "verified@example.com",
            "password": "correct-password",
            "confirm_password": "correct-password",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/check-email")
    assert len(sent_verification_emails) == 1
    email, verification_token = sent_verification_emails[0]
    assert email == "verified@example.com"

    with session_factory() as db:
        user = db.execute(
            select(models.User).where(models.User.email == email)
        ).scalars().one()
        assert user.email_verified_at is None

    verify_page = client.get(f"/verify-email/{verification_token}")
    assert "Confirm your email address" in verify_page.text
    response = client.post(
        f"/verify-email/{verification_token}",
        data={"csrf_token": token},
    )
    assert response.status_code == 200
    assert "Your email is verified" in response.text

    response = client.post(
        "/login",
        data={
            "csrf_token": token,
            "identifier": "verified_user",
            "password": "correct-password",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/account").status_code == 200


def test_password_reset_expires_and_revokes_existing_access(app_client):
    client, session_factory, _, sent_reset_emails = app_client
    with session_factory() as db:
        user = create_verified_user(db, "reset_user", "reset@example.com")
        user.password_hash = hash_password("old-password")
        db.commit()
        old_access_token = create_access_token(user.id, user.auth_version)

    csrf = csrf_token(client)
    response = client.post(
        "/forgot-password",
        data={"csrf_token": csrf, "email": "reset@example.com"},
    )
    assert response.status_code == 200
    assert "reset instructions have been sent" in response.text
    reset_token = sent_reset_emails[0][1]

    response = client.post(
        f"/reset-password/{reset_token}",
        data={
            "csrf_token": csrf,
            "new_password": "new-strong-password",
            "confirm_password": "new-strong-password",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with session_factory() as db:
        user = db.execute(
            select(models.User).where(models.User.email == "reset@example.com")
        ).scalars().one()
        assert verify_password("new-strong-password", user.password_hash)
        assert not verify_password("old-password", user.password_hash)
        assert main.get_user_from_access_token(old_access_token, db) is None
        assert authenticate_user("reset_user", "new-strong-password", db).id == user.id

    reused = client.post(
        f"/reset-password/{reset_token}",
        data={
            "csrf_token": csrf,
            "new_password": "another-password",
            "confirm_password": "another-password",
        },
    )
    assert reused.status_code == 400


def test_login_rate_limit_returns_429(app_client, monkeypatch):
    client, _, _, _ = app_client
    monkeypatch.setattr(settings, "login_rate_limit", 2)
    token = csrf_token(client)

    for _ in range(2):
        response = client.post(
            "/login",
            data={
                "csrf_token": token,
                "identifier": "blocked@example.test",
                "password": "wrong-password",
            },
        )
        assert response.status_code == 401

    response = client.post(
        "/login",
        data={
            "csrf_token": token,
            "identifier": "blocked@example.test",
            "password": "wrong-password",
        },
    )
    assert response.status_code == 429
    assert response.headers["retry-after"]


def test_non_owner_cannot_edit_posts_and_cookie_requests_need_csrf(app_client):
    client, session_factory, _, _ = app_client
    with session_factory() as db:
        author = create_verified_user(db, "post_author", "author@example.com")
        other_user = create_verified_user(db, "post_other", "other@example.com")
        post = Post(title="Original", content="Original content", author=author)
        db.add(post)
        db.commit()
        post_id = post.id
        other_user_id = other_user.id

    token = csrf_token(client)
    with session_factory() as db:
        other_user = db.get(models.User, other_user_id)
        authenticate_client(client, other_user)

    no_csrf = client.put(
        f"/api/Posts/{post_id}",
        json={"title": "Changed", "content": "Changed content"},
    )
    assert no_csrf.status_code == 403

    forbidden = client.put(
        f"/api/Posts/{post_id}",
        json={"title": "Changed", "content": "Changed content"},
        headers={"X-CSRF-Token": token},
    )
    assert forbidden.status_code == 403


def test_api_creates_posts_with_category_and_tags(app_client):
    client, session_factory, _, _ = app_client
    with session_factory() as db:
        user = create_verified_user(db, "api_author", "api@example.com")
        user_id = user.id
        authenticate_client(client, user)
    token = csrf_token(client)

    response = client.post(
        "/api/Posts",
        headers={"X-CSRF-Token": token},
        json={
            "title": "Post with metadata",
            "content": "Full post content",
            "summary": "A concise summary",
            "category": "Web development",
            "tags": ["FastAPI", "Publishing"],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["summary"] == "A concise summary"
    assert payload["category"]["slug"] == "web-development"
    assert {tag["slug"] for tag in payload["tags"]} == {
        "fastapi",
        "publishing",
    }

    with session_factory() as db:
        user = db.get(models.User, user_id)
        assert len(user.posts) == 1


def test_profile_image_upload_validates_and_stores_in_configured_directory(
    app_client,
    tmp_path,
    monkeypatch,
):
    client, session_factory, _, _ = app_client
    with session_factory() as db:
        user = create_verified_user(db, "image_user", "image@example.com")
        user.password_hash = hash_password("image-password")
        db.commit()
        user_id = user.id
        authenticate_client(client, user)
    token = csrf_token(client)
    image_directory = tmp_path / "uploaded"
    monkeypatch.setattr(main, "PROFILE_PICS_DIR", image_directory)
    image_directory.mkdir()

    image = Image.new("RGB", (4, 4), color="blue")
    image_buffer = BytesIO()
    image.save(image_buffer, format="PNG")

    response = client.post(
        "/account",
        data={
            "csrf_token": token,
            "username": "image_user",
            "email": "image@example.com",
        },
        files={
            "profile_picture": ("avatar.png", image_buffer.getvalue(), "image/png")
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as db:
        user = db.get(models.User, user_id)
        assert user.image_file
        assert (image_directory / user.image_file).is_file()

    rejected = client.post(
        "/account",
        data={
            "csrf_token": token,
            "username": "image_user",
            "email": "image@example.com",
        },
        files={"profile_picture": ("avatar.txt", b"not an image", "text/plain")},
    )
    assert rejected.status_code == 400


def test_search_category_tag_excerpt_and_pagination(app_client):
    client, session_factory, _, _ = app_client
    with session_factory() as db:
        author = create_verified_user(db, "search_author", "search@example.com")
        category = models.Category(name="Engineering", slug="engineering")
        tag = models.Tag(name="FastAPI", slug="fastapi")
        db.add_all([category, tag])
        db.flush()
        now = datetime.now(UTC)
        for index in range(23):
            db.add(
                Post(
                    title=f"Searchable post {index:02d}",
                    content=("long body " * 40) if index == 22 else "A short body",
                    summary="A hand-written summary" if index == 22 else None,
                    author=author,
                    category=category if index == 22 else None,
                    tags=[tag] if index == 22 else [],
                    date_posted=now + timedelta(seconds=index),
                )
            )
        db.commit()

    first_page = client.get("/")
    assert first_page.status_code == 200
    assert first_page.text.count("<article") == 10
    assert 'aria-label="Search posts"' in first_page.text
    assert '<circle cx="10.8" cy="10.8" r="6.8"></circle>' in first_page.text
    assert "A hand-written summary" in first_page.text
    assert "long body long body" not in first_page.text

    search = client.get("/?q=Searchable&category=engineering&tag=fastapi")
    assert search.status_code == 200
    assert search.text.count("<article") == 1
    assert "Searchable post 22" in search.text

    missing = client.get("/?q=does-not-exist")
    assert "No posts match your search." in missing.text


def test_profile_search_button_has_accessible_icon(app_client):
    client, session_factory, _, _ = app_client
    with session_factory() as db:
        user = create_verified_user(db, "profile_searcher", "profile@example.com")
        user_id = user.id

    response = client.get(f"/users/{user_id}/posts")

    assert response.status_code == 200
    assert 'aria-label="Search posts"' in response.text
    assert '<circle cx="10.8" cy="10.8" r="6.8"></circle>' in response.text
