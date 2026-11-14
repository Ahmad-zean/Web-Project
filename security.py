import hmac
import secrets

from fastapi import Form, Header, HTTPException, Request, status

from config import settings
from services import ACCESS_TOKEN_COOKIE

CSRF_COOKIE_NAME = (
    "__Host-csrf_token" if settings.app_env == "production" else "csrf_token"
)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _validate_csrf_token(request: Request, submitted_token: str | None) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if (
        not cookie_token
        or not submitted_token
        or not hmac.compare_digest(cookie_token, submitted_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The form security token is missing or invalid. Reload the page and try again.",
        )


def require_form_csrf(
    request: Request,
    csrf_token: str | None = Form(default=None),
) -> None:
    _validate_csrf_token(request, csrf_token)


def require_header_csrf(
    request: Request,
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> None:
    if ACCESS_TOKEN_COOKIE in request.cookies:
        _validate_csrf_token(request, csrf_token)
