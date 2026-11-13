import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from config import settings
from schema import PostCreate, UserCreate, UserUpdate

ACCESS_TOKEN_COOKIE = "access_token"
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes


def create_user_record(user: UserCreate, db: Session) -> models.User:
    existing_username = db.execute(
        select(models.User).where(models.User.username == user.username)
    ).scalars().first()
    if existing_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists",
        )

    existing_email = db.execute(
        select(models.User).where(models.User.email == user.email)
    ).scalars().first()
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already exists",
        )

    new_user = models.User(
        username=user.username,
        email=user.email,
        password_hash=hash_password(user.password),
    )
    db.add(new_user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already exists",
        ) from exc
    db.refresh(new_user)
    return new_user


def register_user(
    user: UserCreate,
    confirm_password: str,
    db: Session,
) -> models.User:
    if user.password != confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passwords do not match",
        )
    return create_user_record(user, db)


def update_user_account(
    user: models.User,
    profile: UserUpdate,
    db: Session,
    *,
    password_hash: str | None = None,
    image_file: str | None = None,
) -> models.User:
    if profile.username is not None:
        existing_username = db.execute(
            select(models.User).where(
                models.User.username == profile.username,
                models.User.id != user.id,
            )
        ).scalars().first()
        if existing_username:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already exists",
            )

    if profile.email is not None:
        existing_email = db.execute(
            select(models.User).where(
                models.User.email == profile.email,
                models.User.id != user.id,
            )
        ).scalars().first()
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already exists",
            )

    if profile.username is not None:
        user.username = profile.username
    if profile.email is not None:
        user.email = profile.email
    if password_hash is not None:
        user.password_hash = password_hash
    if image_file is not None:
        user.image_file = image_file

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already exists",
        ) from exc
    db.refresh(user)
    return user


def authenticate_user(
    identifier: str,
    password: str,
    db: Session,
) -> models.User:
    user = db.execute(
        select(models.User).where(
            or_(
                models.User.username == identifier,
                models.User.email == identifier,
            )
        )
    ).scalars().first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username/email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def create_access_token(user_id: int) -> str:
    expires_at = datetime.now(UTC) + timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES,
    )
    return jwt.encode(
        {"sub": str(user_id), "exp": expires_at},
        settings.secret_key.get_secret_value(),
        algorithm=settings.algorithm,
    )


def get_user_from_access_token(
    token: str | None,
    db: Session,
) -> models.User | None:
    if not token:
        return None

    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.algorithm],
            options={"require": ["exp", "sub"]},
        )
        user_id = int(payload["sub"])
    except (jwt.InvalidTokenError, TypeError, ValueError):
        return None

    return db.get(models.User, user_id)


def get_request_user(request: Request, db: Session) -> models.User | None:
    token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    authorization = request.headers.get("authorization", "")
    if not token and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    return get_user_from_access_token(token, db)


def require_authenticated_user(
    request: Request,
    db: Session,
) -> models.User:
    user = get_request_user(request, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Please log in to continue",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    password_hash = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=64,
    )
    return f"scrypt${salt.hex()}${password_hash.hex()}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    if stored_hash is None:
        return False

    try:
        algorithm, salt_hex, hash_hex = stored_hash.split("$")
        salt = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(hash_hex)
    except ValueError:
        return False

    if algorithm != "scrypt" or len(expected_hash) != 64:
        return False

    actual_hash = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=len(expected_hash),
    )
    return hmac.compare_digest(actual_hash, expected_hash)


def authorize_post_owner(
    post: models.Post,
    user: models.User,
) -> None:
    if post.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only change your own posts",
        )


def create_post_record(
    post: PostCreate,
    user: models.User,
    db: Session,
) -> models.Post:
    new_post = models.Post(
        author=user,
        title=post.title,
        content=post.content,
    )
    db.add(new_post)
    db.commit()
    db.refresh(new_post)
    return new_post
