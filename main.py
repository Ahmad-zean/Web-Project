import logging
from io import BytesIO
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from fastapi import Depends, FastAPI, Request, HTTPException, Form, File, UploadFile, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.exceptions import HTTPException as StarletteHTTPException
from schema import PasswordChange, PostCreate, PostPatch, UserCreate, UserUpdate
import models
from database import Base, engine, get_db, SessionLocal
from pydantic import ValidationError
from starlette.responses import RedirectResponse
from Routers.Post import router as post_router
from Routers.User import router as user_router
from services import (
    ACCESS_TOKEN_COOKIE,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    authenticate_user,
    authorize_post_owner,
    create_access_token,
    create_post_record,
    get_user_from_access_token,
    get_request_user,
    hash_password,
    require_authenticated_user,
    register_user,
    update_user_account,
    verify_password,
)

logger = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent
MEDIA_DIR = APP_DIR / "media"
PROFILE_PICS_DIR = MEDIA_DIR / "profile_pics"
MAX_PROFILE_PICTURE_BYTES = 5 * 1024 * 1024
MAX_PROFILE_PICTURE_DIMENSION = 4096

Base.metadata.create_all(bind=engine)
with engine.begin() as connection:
    user_columns = {column["name"] for column in inspect(connection).get_columns("users")}
    if "password_hash" not in user_columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)"))


app = FastAPI()
app.include_router(user_router)
app.include_router(post_router)
app.mount("/static", StaticFiles(directory="static"), name="static")
PROFILE_PICS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

templates = Jinja2Templates(directory="Templates")


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
    token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    request.state.current_user = None
    if token:
        with SessionLocal() as db:
            request.state.current_user = get_user_from_access_token(token, db)
    return await call_next(request)


#error handler for 404 and other errors
@app.exception_handler(StarletteHTTPException)
def http_error_page(request: Request, exc: StarletteHTTPException):
    return templates.TemplateResponse(
        request,
        "error.html",
        {"title": "Error", "status_code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code,
    )

#home page and posts page
@app.get("/", name="home")
@app.get("/posts", name="posts")
def home(request: Request, db: Session = Depends(get_db)):
    result = db.execute(select(models.Post).order_by(models.Post.date_posted.desc()))
    posts = result.scalars().all()
    return templates.TemplateResponse(
        request, "home.html",
        {"request": request,
        "posts": posts,
        "title": "Home"})

#get post page
@app.get("/posts/{post_id}", include_in_schema=False)
def post_page(request: Request, post_id: int, db: Annotated[Session, Depends(get_db)]):
    result = db.execute(select(models.Post).where(models.Post.id == post_id))
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
            "error": None,
        },
    )


@app.post("/posts/{post_id}/edit", include_in_schema=False)
def submit_edit_post_page(
    request: Request,
    post_id: int,
    title: str = Form(),
    content: str = Form(),
    db: Session = Depends(get_db),
):
    post = db.get(models.Post, post_id)
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    user = require_authenticated_user(request, db)
    authorize_post_owner(post, user)

    try:
        patch = PostPatch.model_validate(
            {
                "title": title if title != post.title else None,
                "content": content if content != post.content else None,
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
                "error": exc.errors()[0]["msg"],
            },
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    if patch.title is None and patch.content is None:
        error = "Change the title or content before saving."
        response_status = status.HTTP_400_BAD_REQUEST
    else:
        if patch.title is not None:
            post.title = patch.title
        if patch.content is not None:
            post.content = patch.content
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
):
    result = db.execute(select(models.User).where(models.User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    result = db.execute(select(models.Post).where(models.Post.user_id == user_id))
    posts = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "user_post_page.html",
        {"posts": posts, "user": user, "title": f"{user.username}'s Posts"},
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
    response = RedirectResponse(
        url=request.url_for("home"),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=create_access_token(created_user.id),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return response


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
):
    try:
        user = authenticate_user(identifier, password, db)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"title": "Login", "identifier": identifier, "error": exc.detail},
            status_code=exc.status_code,
        )

    response = RedirectResponse(
        url=request.url_for("home"),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=create_access_token(user.id),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return response


@app.post("/logout", include_in_schema=False, name="logout")
def logout(request: Request):
    response = RedirectResponse(
        url=request.url_for("home"),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.delete_cookie(
        ACCESS_TOKEN_COOKIE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
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

    return RedirectResponse(
        url=request.url_for("account_page").include_query_params(updated="1"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/post/create", include_in_schema=False, name="create_post_page")
def create_post_page(request: Request, db: Session = Depends(get_db)):
    user = require_authenticated_user(request, db)
    return templates.TemplateResponse(
        request,
        "create_post.html",
        {
            "title": "Create Post",
            "author": user.username,
            "post_title": "",
            "content": "",
            "error": None,
        },
    )


#fetch a database session and create a post via frontend form submission
@app.post("/post/create", include_in_schema=False)
def submit_create_post_page(
    request: Request,
    title: str = Form(),
    content: str = Form(),
    db: Session = Depends(get_db),
):
    user = require_authenticated_user(request, db)
    try:
        post = PostCreate.model_validate(
            {
                "title": title,
                "content": content,
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
                "error": exc.detail,
            },
            status_code=exc.status_code,
        )
    return RedirectResponse(
        url=request.url_for("post_page", post_id=new_post.id),
        status_code=status.HTTP_303_SEE_OTHER,
    )
