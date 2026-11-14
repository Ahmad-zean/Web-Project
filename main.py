import logging
from io import BytesIO
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session, selectinload

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from schema import (
    PasswordChange,
    PasswordReset,
    PostCreate,
    PostUpdate,
    UserCreate,
    UserUpdate,
)
import models
from config import settings
from database import get_db, SessionLocal
from pydantic import EmailStr, TypeAdapter, ValidationError
from starlette.responses import RedirectResponse
from Routers.Post import router as post_router
from Routers.User import router as user_router
from email_service import (
    EmailDeliveryError,
    send_password_reset_email,
    send_verification_email,
)
from security import (
    CSRF_COOKIE_NAME,
    new_csrf_token,
    require_form_csrf,
)
from services import (
    ACCESS_TOKEN_COOKIE,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    EMAIL_VERIFICATION,
    PASSWORD_RESET,
    authenticate_user,
    apply_post_metadata,
    authorize_post_owner,
    clear_login_rate_limit,
    create_access_token,
    create_email_action_token,
    create_post_record,
    consume_email_verification_token,
    enforce_login_rate_limit,
    get_user_from_access_token,
    get_request_user,
    hash_password,
    login_rate_limit_keys,
    record_failed_login,
    require_authenticated_user,
    register_user,
    reset_user_password,
    update_user_account,
    verify_password,
    verify_email_action_token,
)

logger = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent
MEDIA_DIR = APP_DIR / "media"
PROFILE_PICS_DIR = MEDIA_DIR / "profile_pics"
MAX_PROFILE_PICTURE_BYTES = 5 * 1024 * 1024
MAX_PROFILE_PICTURE_DIMENSION = 4096

app = FastAPI()
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.trusted_host_list or ["*"],
)
app.include_router(user_router)
app.include_router(post_router)
PROFILE_PICS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")


def csrf_context(request: Request) -> dict[str, str]:
    return {"csrf_token": getattr(request.state, "csrf_token", "")}


templates = Jinja2Templates(
    directory=str(APP_DIR / "Templates"),
    context_processors=[csrf_context],
)
POSTS_PER_PAGE = 10


def paginate_posts(
    db: Session,
    page: int,
    *,
    user_id: int | None = None,
    query: str | None = None,
    category: str | None = None,
    tag: str | None = None,
) -> tuple[list[models.Post], int, int, list[int]]:
    post_query = select(models.Post)
    count_query = select(func.count(models.Post.id))
    filters = []
    if user_id is not None:
        filters.append(models.Post.user_id == user_id)
    if query:
        escaped_query = (
            query.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        search_pattern = f"%{escaped_query}%"
        filters.append(
            or_(
                models.Post.title.ilike(search_pattern, escape="\\"),
                models.Post.content.ilike(search_pattern, escape="\\"),
                models.Post.summary.ilike(search_pattern, escape="\\"),
            )
        )
    if category:
        filters.append(models.Post.category.has(models.Category.slug == category))
    if tag:
        filters.append(models.Post.tags.any(models.Tag.slug == tag))
    if filters:
        post_query = post_query.where(*filters)
        count_query = count_query.where(*filters)

    total_posts = db.scalar(count_query) or 0
    total_pages = max(1, (total_posts + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
    current_page = min(page, total_pages)
    if user_id is None:
        post_query = post_query.options(selectinload(models.Post.author))
    post_query = post_query.options(
        selectinload(models.Post.category),
        selectinload(models.Post.tags),
    )
    posts = db.execute(
        post_query.order_by(models.Post.date_posted.desc(), models.Post.id.desc())
        .offset((current_page - 1) * POSTS_PER_PAGE)
        .limit(POSTS_PER_PAGE)
    ).scalars().all()
    page_numbers = list(
        range(max(1, current_page - 2), min(total_pages, current_page + 2) + 1)
    )
    return posts, current_page, total_pages, page_numbers


def get_browse_options(db: Session) -> tuple[list[models.Category], list[models.Tag]]:
    categories = db.execute(
        select(models.Category).order_by(models.Category.name)
    ).scalars().all()
    tags = db.execute(select(models.Tag).order_by(models.Tag.name)).scalars().all()
    return categories, tags


def set_access_token_cookie(response, user: models.User) -> None:
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=create_access_token(user.id, user.auth_version),
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )


def send_verification_link(user: models.User, db: Session) -> None:
    token = create_email_action_token(user, EMAIL_VERIFICATION, db)
    send_verification_email(user.email, token)


def remove_profile_picture(filename: str) -> None:
    if not filename or Path(filename).name != filename:
        return
    try:
        (PROFILE_PICS_DIR / filename).unlink(missing_ok=True)
    except OSError:
        logger.exception("Could not remove profile picture %s", filename)


def store_profile_picture(upload: UploadFile) -> str:
    contents = upload.file.read(MAX_PROFILE_PICTURE_BYTES + 1)
    if len(contents) > MAX_PROFILE_PICTURE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Profile pictures must be 5 MB or smaller.",
        )

    try:
        with Image.open(BytesIO(contents)) as image:
            image_format = image.format
            if image_format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Upload a JPEG, PNG, or WebP image.",
                )
            if (
                image.width > MAX_PROFILE_PICTURE_DIMENSION
                or image.height > MAX_PROFILE_PICTURE_DIMENSION
                or image.width * image.height > 16_000_000
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Profile picture dimensions are too large.",
                )
            image.verify()

        with Image.open(BytesIO(contents)) as image:
            image = ImageOps.exif_transpose(image)
            if image_format == "JPEG" and image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            elif image_format == "WEBP" and image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            output = BytesIO()
            image.save(output, format=image_format)
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file is not a valid image.",
        ) from exc

    extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[image_format]
    filename = f"{uuid4().hex}.{extension}"
    try:
        with (PROFILE_PICS_DIR / filename).open("xb") as image_file:
            image_file.write(output.getvalue())
    except OSError as exc:
        remove_profile_picture(filename)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not save the profile picture.",
        ) from exc
    return filename


@app.middleware("http")
async def load_current_user(request: Request, call_next):
    csrf_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not csrf_token or len(csrf_token) > 128:
        csrf_token = new_csrf_token()
    request.state.csrf_token = csrf_token
    token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    request.state.current_user = None
    if token:
        with SessionLocal() as db:
            request.state.current_user = get_user_from_access_token(token, db)
    response = await call_next(request)
    if request.cookies.get(CSRF_COOKIE_NAME) != csrf_token:
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=csrf_token,
            httponly=True,
            secure=settings.app_env == "production",
            samesite="strict",
            max_age=60 * 60 * 24,
            path="/",
        )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if settings.app_env == "production":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


@app.get("/health/live", include_in_schema=False)
def health_live():
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
def health_ready(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


#error handler for 404 and other errors
@app.exception_handler(StarletteHTTPException)
def http_error_page(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"detail": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )
    return templates.TemplateResponse(
        request,
        "error.html",
        {"title": "Error", "status_code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code,
    )

#home page and posts page
@app.get("/", name="home")
@app.get("/posts", name="posts")
def home(
    request: Request,
    page: int = Query(default=1, ge=1),
    q: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=60),
    tag: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
):
    q = q.strip() if q and q.strip() else None
    category = category.strip() if category else None
    tag = tag.strip() if tag else None
    posts, page, total_pages, page_numbers = paginate_posts(
        db,
        page,
        query=q,
        category=category,
        tag=tag,
    )
    categories, tags = get_browse_options(db)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "posts": posts,
            "page": page,
            "total_pages": total_pages,
            "page_numbers": page_numbers,
            "categories": categories,
            "tags": tags,
            "q": q or "",
            "selected_category": category or "",
            "selected_tag": tag or "",
            "title": "Home",
        },
    )

#get post page
@app.get("/posts/{post_id}", include_in_schema=False)
def post_page(request: Request, post_id: int, db: Annotated[Session, Depends(get_db)]):
    result = db.execute(
        select(models.Post)
        .options(
            selectinload(models.Post.author),
            selectinload(models.Post.category),
            selectinload(models.Post.tags),
        )
        .where(models.Post.id == post_id)
    )
    post = result.scalars().first()
    if post:
        title = post.title[:50]
        return templates.TemplateResponse(
            request,
            "post.html",
            {"post": post, "title": title},
        )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")


@app.get("/posts/{post_id}/edit", include_in_schema=False, name="edit_post_page")
def edit_post_page(
    request: Request,
    post_id: int,
    db: Session = Depends(get_db),
):
    post = db.get(models.Post, post_id)
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    user = require_authenticated_user(request, db)
    authorize_post_owner(post, user)
    return templates.TemplateResponse(
        request,
        "edit_post.html",
        {
            "post": post,
            "title": f"Edit {post.title[:50]}",
            "post_title": post.title,
            "content": post.content,
            "summary": post.summary or "",
            "category": post.category.name if post.category else "",
            "tags": ", ".join(tag.name for tag in post.tags),
            "error": None,
        },
    )


@app.post("/posts/{post_id}/edit", include_in_schema=False)
def submit_edit_post_page(
    request: Request,
    post_id: int,
    title: str = Form(),
    content: str = Form(),
    summary: str = Form(default=""),
    category: str = Form(default=""),
    tags: str = Form(default=""),
    db: Session = Depends(get_db),
    _: None = Depends(require_form_csrf),
):
    post = db.get(models.Post, post_id)
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    user = require_authenticated_user(request, db)
    authorize_post_owner(post, user)

    try:
        post_update = PostUpdate.model_validate(
            {
                "title": title,
                "content": content,
                "summary": summary.strip() or None,
                "category": category.strip() or None,
                "tags": [tag.strip() for tag in tags.split(",") if tag.strip()],
            }
        )
    except ValidationError as exc:
        return templates.TemplateResponse(
            request,
            "edit_post.html",
            {
                "post": post,
                "title": f"Edit {post.title[:50]}",
                "post_title": title,
                "content": content,
                "summary": summary,
                "category": category,
                "tags": tags,
                "error": exc.errors()[0]["msg"],
            },
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    existing_tags = {tag.name.casefold() for tag in post.tags}
    updated_tags = {tag.casefold() for tag in post_update.tags}
    existing_category = post.category.name if post.category else None
    has_changes = (
        post.title != post_update.title
        or post.content != post_update.content
        or post.summary != post_update.summary
        or existing_category != post_update.category
        or existing_tags != updated_tags
    )
    if not has_changes:
        error = "Change at least one field before saving."
        response_status = status.HTTP_400_BAD_REQUEST
    else:
        post.title = post_update.title
        post.content = post_update.content
        post.summary = post_update.summary
        apply_post_metadata(
            post,
            db,
            category_name=post_update.category,
            tag_names=post_update.tags,
        )
        db.commit()
        return RedirectResponse(
            url=request.url_for("post_page", post_id=post.id),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    return templates.TemplateResponse(
        request,
        "edit_post.html",
        {
            "post": post,
            "title": f"Edit {post.title[:50]}",
            "post_title": title,
            "content": content,
            "summary": summary,
            "category": category,
            "tags": tags,
            "error": error,
        },
        status_code=response_status,
    )


@app.get("/posts/{post_id}/delete", include_in_schema=False, name="delete_post_page")
def delete_post_page(
    request: Request,
    post_id: int,
    db: Session = Depends(get_db),
):
    post = db.get(models.Post, post_id)
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    user = require_authenticated_user(request, db)
    authorize_post_owner(post, user)
    return templates.TemplateResponse(
        request,
        "delete_post.html",
        {"post": post, "title": "Delete Post", "error": None},
    )


@app.post("/posts/{post_id}/delete", include_in_schema=False)
def submit_delete_post_page(
    request: Request,
    post_id: int,
    db: Session = Depends(get_db),
    _: None = Depends(require_form_csrf),
):
    post = db.get(models.Post, post_id)
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    user = require_authenticated_user(request, db)
    authorize_post_owner(post, user)
    db.delete(post)
    db.commit()
    return RedirectResponse(
        url=request.url_for("home"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


#get user posts page
@app.get("/users/{user_id}/posts", include_in_schema=False, name="user_post_page")
def user_post_page(
    request: Request,
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    q: str | None = Query(default=None, max_length=100),
    category: str | None = Query(default=None, max_length=60),
    tag: str | None = Query(default=None, max_length=50),
):
    result = db.execute(select(models.User).where(models.User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    q = q.strip() if q and q.strip() else None
    category = category.strip() if category else None
    tag = tag.strip() if tag else None
    posts, page, total_pages, page_numbers = paginate_posts(
        db,
        page,
        user_id=user_id,
        query=q,
        category=category,
        tag=tag,
    )
    categories, tags = get_browse_options(db)
    return templates.TemplateResponse(
        request,
        "user_post_page.html",
        {
            "posts": posts,
            "user": user,
            "page": page,
            "total_pages": total_pages,
            "page_numbers": page_numbers,
            "categories": categories,
            "tags": tags,
            "q": q or "",
            "selected_category": category or "",
            "selected_tag": tag or "",
            "title": f"{user.username}'s Posts",
        },
    )

#user registration page
@app.get("/register", include_in_schema=False, name="register_page")
@app.get("/users/create", include_in_schema=False, name="create_user_page")
def create_user_page(request: Request):
    return templates.TemplateResponse(
        request,
        "create_user.html",
        {
            "title": "Register",
            "username": "",
            "email": "",
            "error": None,
        },
    )

#register a user via frontend form submission
@app.post("/register", include_in_schema=False, name="submit_register_page")
@app.post("/users/create", include_in_schema=False, name="submit_create_user_page")
def submit_register_page(
    request: Request,
    username: str = Form(),
    email: str = Form(),
    password: str = Form(),
    confirm_password: str = Form(),
    db: Session = Depends(get_db),
    _: None = Depends(require_form_csrf),
):
    try:
        user = UserCreate.model_validate(
            {"username": username, "email": email, "password": password}
        )
        #page is reload due to validation error, so we need to return the form with the error message
    except ValidationError as exc:
        return templates.TemplateResponse(
            request,
            "create_user.html",
            {
                "title": "Register",
                "username": username,
                "email": email,
                "error": exc.errors()[0]["msg"],
            },
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    rate_limit_keys = login_rate_limit_keys(request, f"register:{user.email}")
    try:
        enforce_login_rate_limit(db, rate_limit_keys)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "create_user.html",
            {
                "title": "Register",
                "username": username,
                "email": email,
                "error": exc.detail,
            },
            status_code=exc.status_code,
        )
    record_failed_login(db, rate_limit_keys)
    try:
        created_user = register_user(user, confirm_password, db)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "create_user.html",
            {
                "title": "Register",
                "username": username,
                "email": email,
                "error": exc.detail,
            },
            status_code=exc.status_code,
        )
    try:
        send_verification_link(created_user, db)
    except EmailDeliveryError:
        return templates.TemplateResponse(
            request,
            "create_user.html",
            {
                "title": "Register",
                "username": username,
                "email": email,
                "error": (
                    "Your account was created, but the verification email could not "
                    "be sent. Use the resend verification page to try again."
                ),
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return RedirectResponse(
        url=request.url_for("check_email_page"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/check-email", include_in_schema=False, name="check_email_page")
def check_email_page(request: Request):
    return templates.TemplateResponse(
        request,
        "check_email.html",
        {"title": "Check your email"},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/email/resend-verification", include_in_schema=False)
def resend_verification_email_page(
    request: Request,
    email: str = Form(),
    db: Session = Depends(get_db),
    _: None = Depends(require_form_csrf),
):
    email = email.strip()
    rate_limit_keys = login_rate_limit_keys(request, f"verify:{email}")
    try:
        enforce_login_rate_limit(db, rate_limit_keys)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "check_email.html",
            {"title": "Check your email", "error": exc.detail, "email": email},
            status_code=exc.status_code,
        )
    record_failed_login(db, rate_limit_keys)
    user = db.execute(
        select(models.User).where(func.lower(models.User.email) == email.casefold())
    ).scalars().first()
    if user and user.email_verified_at is None:
        try:
            send_verification_link(user, db)
        except EmailDeliveryError:
            return templates.TemplateResponse(
                request,
                "check_email.html",
                {
                    "title": "Check your email",
                    "email": email,
                    "error": "The verification email could not be sent. Please try again later.",
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
    return templates.TemplateResponse(
        request,
        "check_email.html",
        {"title": "Check your email", "email": email, "sent": True},
    )


@app.get("/verify-email/{token}", include_in_schema=False, name="verify_email_page")
def verify_email_page(request: Request, token: str, db: Session = Depends(get_db)):
    valid = verify_email_action_token(token, EMAIL_VERIFICATION, db) is not None
    return templates.TemplateResponse(
        request,
        "verify_email.html",
        {"title": "Verify email", "token": token, "valid": valid, "verified": False},
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.post("/verify-email/{token}", include_in_schema=False)
def submit_verify_email_page(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_form_csrf),
):
    verified = consume_email_verification_token(token, db)
    return templates.TemplateResponse(
        request,
        "verify_email.html",
        {
            "title": "Verify email",
            "token": token,
            "valid": False,
            "verified": verified,
        },
        status_code=(
            status.HTTP_200_OK
            if verified
            else status.HTTP_400_BAD_REQUEST
        ),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.get("/forgot-password", include_in_schema=False, name="forgot_password_page")
def forgot_password_page(request: Request):
    return templates.TemplateResponse(
        request,
        "forgot_password.html",
        {"title": "Reset password", "email": "", "sent": False},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/forgot-password", include_in_schema=False, name="submit_forgot_password_page")
def submit_forgot_password_page(
    request: Request,
    email: str = Form(),
    db: Session = Depends(get_db),
    _: None = Depends(require_form_csrf),
):
    try:
        normalized_email = str(TypeAdapter(EmailStr).validate_python(email.strip()))
    except ValidationError:
        return templates.TemplateResponse(
            request,
            "forgot_password.html",
            {
                "title": "Reset password",
                "email": email,
                "sent": False,
                "error": "Enter a valid email address.",
            },
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    rate_limit_keys = login_rate_limit_keys(request, f"reset:{normalized_email}")
    try:
        enforce_login_rate_limit(db, rate_limit_keys)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "forgot_password.html",
            {
                "title": "Reset password",
                "email": normalized_email,
                "sent": False,
                "error": exc.detail,
            },
            status_code=exc.status_code,
        )
    record_failed_login(db, rate_limit_keys)
    user = db.execute(
        select(models.User).where(
            func.lower(models.User.email) == normalized_email.casefold(),
            models.User.email_verified_at.is_not(None),
        )
    ).scalars().first()
    if user:
        token = create_email_action_token(user, PASSWORD_RESET, db)
        try:
            send_password_reset_email(user.email, token)
        except EmailDeliveryError:
            return templates.TemplateResponse(
                request,
                "forgot_password.html",
                {
                    "title": "Reset password",
                    "email": normalized_email,
                    "sent": False,
                    "error": "The reset email could not be sent. Please try again later.",
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
    return templates.TemplateResponse(
        request,
        "forgot_password.html",
        {"title": "Reset password", "email": normalized_email, "sent": True},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/reset-password/{token}", include_in_schema=False, name="reset_password_page")
def reset_password_page(request: Request, token: str, db: Session = Depends(get_db)):
    valid = verify_email_action_token(token, PASSWORD_RESET, db) is not None
    return templates.TemplateResponse(
        request,
        "reset_password.html",
        {"title": "Choose a new password", "token": token, "valid": valid},
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.post("/reset-password/{token}", include_in_schema=False)
def submit_reset_password_page(
    request: Request,
    token: str,
    new_password: str = Form(),
    confirm_password: str = Form(),
    db: Session = Depends(get_db),
    _: None = Depends(require_form_csrf),
):
    try:
        password_reset = PasswordReset.model_validate(
            {"new_password": new_password, "confirm_password": confirm_password}
        )
    except ValidationError as exc:
        valid = verify_email_action_token(token, PASSWORD_RESET, db) is not None
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {
                "title": "Choose a new password",
                "token": token,
                "valid": valid,
                "error": exc.errors()[0]["msg"],
            },
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    if password_reset.new_password != password_reset.confirm_password:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {
                "title": "Choose a new password",
                "token": token,
                "valid": True,
                "error": "The new password and confirmation do not match.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    if not reset_user_password(token, password_reset.new_password, db):
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {
                "title": "Choose a new password",
                "token": token,
                "valid": False,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    return RedirectResponse(
        url=request.url_for("login_page"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/login", include_in_schema=False, name="login_page")
def login_page(request: Request):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"title": "Login", "identifier": "", "error": None},
    )


@app.post("/login", include_in_schema=False, name="submit_login_page")
def submit_login_page(
    request: Request,
    identifier: str = Form(),
    password: str = Form(),
    db: Session = Depends(get_db),
    _: None = Depends(require_form_csrf),
):
    rate_limit_keys = login_rate_limit_keys(request, identifier)
    try:
        enforce_login_rate_limit(db, rate_limit_keys)
        user = authenticate_user(identifier, password, db)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            record_failed_login(db, rate_limit_keys)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"title": "Login", "identifier": identifier, "error": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )
    clear_login_rate_limit(db, rate_limit_keys)

    response = RedirectResponse(
        url=request.url_for("home"),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    set_access_token_cookie(response, user)
    return response


@app.post("/logout", include_in_schema=False, name="logout")
def logout(
    request: Request,
    _: None = Depends(require_form_csrf),
):
    response = RedirectResponse(
        url=request.url_for("home"),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.delete_cookie(
        ACCESS_TOKEN_COOKIE,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        path="/",
    )
    return response


@app.get("/account", include_in_schema=False, name="account_page")
def account_page(
    request: Request,
    db: Session = Depends(get_db),
    updated: bool = False,
):
    user = get_request_user(request, db)
    if not user:
        return RedirectResponse(
            url=request.url_for("login_page"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return render_account_page(
        request,
        user,
        username=user.username,
        email=user.email,
        success="Your account has been updated." if updated else None,
    )


def render_account_page(
    request: Request,
    user: models.User,
    *,
    username: str,
    email: str,
    error: str | None = None,
    success: str | None = None,
    status_code: int = status.HTTP_200_OK,
):
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "title": "Account",
            "user": user,
            "username": username,
            "email": email,
            "error": error,
            "success": success,
        },
        status_code=status_code,
    )


@app.post("/account", include_in_schema=False, name="update_account_page")
def update_account_page(
    request: Request,
    username: str = Form(),
    email: str = Form(),
    current_password: str = Form(default=""),
    new_password: str = Form(default=""),
    confirm_password: str = Form(default=""),
    profile_picture: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
    _: None = Depends(require_form_csrf),
):
    user = get_request_user(request, db)
    if not user:
        return RedirectResponse(
            url=request.url_for("login_page"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    try:
        profile = UserUpdate.model_validate(
            {"username": username.strip(), "email": email.strip()}
        )
    except ValidationError as exc:
        return render_account_page(
            request,
            user,
            username=username,
            email=email,
            error=exc.errors()[0]["msg"],
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    password_hash = None
    if current_password or new_password or confirm_password:
        try:
            password_change = PasswordChange.model_validate(
                {
                    "current_password": current_password,
                    "new_password": new_password,
                    "confirm_password": confirm_password,
                }
            )
        except ValidationError as exc:
            return render_account_page(
                request,
                user,
                username=username,
                email=email,
                error=exc.errors()[0]["msg"],
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

        if password_change.new_password != password_change.confirm_password:
            return render_account_page(
                request,
                user,
                username=username,
                email=email,
                error="The new password and confirmation do not match.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if not verify_password(password_change.current_password, user.password_hash):
            return render_account_page(
                request,
                user,
                username=username,
                email=email,
                error="The current password is incorrect.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        password_hash = hash_password(password_change.new_password)

    new_image_file = None
    if profile_picture and profile_picture.filename:
        try:
            new_image_file = store_profile_picture(profile_picture)
        except HTTPException as exc:
            return render_account_page(
                request,
                user,
                username=username,
                email=email,
                error=exc.detail,
                status_code=exc.status_code,
            )

    old_image_file = user.image_file
    email_changed = profile.email != user.email
    update_succeeded = False
    try:
        try:
            update_user_account(
                user,
                profile,
                db,
                password_hash=password_hash,
                image_file=new_image_file,
            )
        except HTTPException as exc:
            return render_account_page(
                request,
                user,
                username=username,
                email=email,
                error=exc.detail,
                status_code=exc.status_code,
            )
        update_succeeded = True
    finally:
        if new_image_file and not update_succeeded:
            remove_profile_picture(new_image_file)

    if new_image_file and old_image_file:
        remove_profile_picture(old_image_file)

    if email_changed:
        try:
            send_verification_link(user, db)
        except EmailDeliveryError:
            return templates.TemplateResponse(
                request,
                "check_email.html",
                {
                    "title": "Verify your new email",
                    "email": user.email,
                    "error": (
                        "Your email was changed, but the verification message could "
                        "not be sent. Request another message below."
                    ),
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return RedirectResponse(
            url=request.url_for("check_email_page"),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    response = RedirectResponse(
        url=request.url_for("account_page").include_query_params(updated="1"),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    set_access_token_cookie(response, user)
    return response


@app.get("/post/create", include_in_schema=False, name="create_post_page")
def create_post_page(request: Request, db: Session = Depends(get_db)):
    user = require_authenticated_user(request, db)
    categories, _ = get_browse_options(db)
    return templates.TemplateResponse(
        request,
        "create_post.html",
        {
            "title": "Create Post",
            "author": user.username,
            "post_title": "",
            "content": "",
            "summary": "",
            "category": "",
            "tags": "",
            "categories": categories,
            "error": None,
        },
    )


#fetch a database session and create a post via frontend form submission
@app.post("/post/create", include_in_schema=False)
def submit_create_post_page(
    request: Request,
    title: str = Form(),
    content: str = Form(),
    summary: str = Form(default=""),
    category: str = Form(default=""),
    tags: str = Form(default=""),
    db: Session = Depends(get_db),
    _: None = Depends(require_form_csrf),
):
    user = require_authenticated_user(request, db)
    try:
        post = PostCreate.model_validate(
            {
                "title": title,
                "content": content,
                "summary": summary.strip() or None,
                "category": category.strip() or None,
                "tags": [tag.strip() for tag in tags.split(",") if tag.strip()],
            }
        )
    except ValidationError as exc:
        return templates.TemplateResponse(
            request,
            "create_post.html",
            {
                "title": "Create Post",
                "author": user.username,
                "post_title": title,
                "content": content,
                "summary": summary,
                "category": category,
                "tags": tags,
                "error": exc.errors()[0]["msg"],
            },
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    try:
        new_post = create_post_record(post, user, db)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "create_post.html",
            {
                "title": "Create Post",
                "author": user.username,
                "post_title": title,
                "content": content,
                "summary": summary,
                "category": category,
                "tags": tags,
                "error": exc.detail,
            },
            status_code=exc.status_code,
        )
    return RedirectResponse(
        url=request.url_for("post_page", post_id=new_post.id),
        status_code=status.HTTP_303_SEE_OTHER,
    )
