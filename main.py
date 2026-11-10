import hashlib
import hmac
import secrets
from typing import Annotated
from xml.etree.ElementInclude import include

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session
from unittest import result

from fastapi import Depends, FastAPI, Request, HTTPException, Form, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from schema import PostResponse, PostCreate, PostUpdate, PostPatch, UserResponse, UserCreate
from datetime import datetime
import models
from database import Base, engine, get_db
from pydantic import ValidationError
from starlette.responses import RedirectResponse
Base.metadata.create_all(bind=engine)
with engine.begin() as connection:
    user_columns = {column["name"] for column in inspect(connection).get_columns("users")}
    if "password_hash" not in user_columns:
        connection.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)"))


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="Templates")

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
    author: str = Form(),
    password: str = Form(),
    title: str = Form(),
    content: str = Form(),
    db: Session = Depends(get_db),
):
    post = db.get(models.Post, post_id)
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    try:
        patch = PostPatch.model_validate(
            {
                "author": author,
                "password": password,
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
        try:
            _authorize_post_author(db, post, patch.author, patch.password)
        except HTTPException as exc:
            error = exc.detail
            response_status = exc.status_code
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
    return templates.TemplateResponse(
        request,
        "delete_post.html",
        {"post": post, "title": "Delete Post", "error": None},
    )


@app.post("/posts/{post_id}/delete", include_in_schema=False)
def submit_delete_post_page(
    request: Request,
    post_id: int,
    author: str = Form(),
    password: str = Form(),
    db: Session = Depends(get_db),
):
    post = db.get(models.Post, post_id)
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    try:
        _authorize_post_author(db, post, author, password)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "delete_post.html",
            {"post": post, "title": "Delete Post", "error": exc.detail},
            status_code=exc.status_code,
        )
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

#user creation page
@app.get("/users/create", include_in_schema=False, name="create_user_page")
def create_user_page(request: Request):
    return templates.TemplateResponse(
        request,
        "create_user.html",
        {
            "title": "Create User",
            "username": "",
            "email": "",
            "error": None,
        },
    )

#fetch a database session and create a user via frontend form submission
@app.post("/users/create", include_in_schema=False)
def submit_create_user_page(
    request: Request,
    username: str = Form(),
    email: str = Form(),
    password: str = Form(),
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
                "title": "Create User",
                "username": username,
                "email": email,
                "error": exc.errors()[0]["msg"],
            },
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    try:
        #pass both data to the function that checks if user exists and creates a new user if not
        created_user = _create_user(user, db)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "create_user.html",
            {
                "title": "Create User",
                "username": username,
                "email": email,
                "error": exc.detail,
            },
            status_code=exc.status_code,
        )
    return RedirectResponse(
        url=request.url_for("user_post_page", user_id=created_user.id),
        status_code=status.HTTP_303_SEE_OTHER,
    )

#check if user exists and create user
def _create_user(user: UserCreate, db: Session):
    result_name = db.execute(
        select(models.User).where(models.User.username == user.username)
    )
    if result_name.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists",
        )

    result_email = db.execute(
        select(models.User).where(models.User.email == user.email)
    )
    if result_email.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already exists",
        )

    new_user = models.User(
        username=user.username,
        email=user.email,
        password_hash=_hash_password(user.password),
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user

#hash the password using scrypt and return the hash
def _hash_password(password: str) -> str:
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


def _verify_password(password: str, stored_hash: str | None) -> bool:
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


def _authorize_post_author(
    db: Session,
    post: models.Post,
    author: str,
    password: str,
) -> None:
    user = db.execute(
        select(models.User).where(models.User.username == author)
    ).scalars().first()
    if not user or post.user_id != user.id or not _verify_password(
        password,
        user.password_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid author or password for this post",
        )


#route to create a user via API
@app.post(
    "/api/Users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_user(user: UserCreate, db: Session = Depends(get_db)):
    return _create_user(user, db)

#route to get a user via API
@app.get("/api/Users/{user_id}", response_model=UserResponse)
def read_user(user_id: int, db: Session = Depends(get_db)):
    result = db.execute(select(models.User).where(models.User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user


#create post page
@app.get("/post/create", include_in_schema=False, name="create_post_page")
def create_post_page(request: Request, db: Session = Depends(get_db)):
    users = db.execute(select(models.User).order_by(models.User.username)).scalars().all()
    return templates.TemplateResponse(
        request,
        "create_post.html",
        {
            "title": "Create Post",
            "users": users,
            "author": "",
            "post_title": "",
            "content": "",
            "error": None,
        },
    )

#fetch a database session and create a post via frontend form submission
@app.post("/post/create", include_in_schema=False)
def submit_create_post_page(
    request: Request,
    author: str = Form(),
    password: str = Form(),
    title: str = Form(),
    content: str = Form(),
    db: Session = Depends(get_db),
):
    try:
        post = PostCreate.model_validate(
            {
                "author": author,
                "password": password,
                "title": title,
                "content": content,
            }
        )
    except ValidationError as exc:
        users = db.execute(select(models.User).order_by(models.User.username)).scalars().all()
        return templates.TemplateResponse(
            request,
            "create_post.html",
            {
                "title": "Create Post",
                "users": users,
                "author": author,
                "post_title": title,
                "content": content,
                "error": exc.errors()[0]["msg"],
            },
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    try:
        new_post = _create_post(post, db)
    except HTTPException as exc:
        users = db.execute(select(models.User).order_by(models.User.username)).scalars().all()
        return templates.TemplateResponse(
            request,
            "create_post.html",
            {
                "title": "Create Post",
                "users": users,
                "author": author,
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

#pass the post data and database session to the function that checks if the author exists and creates a new post if so
def _create_post(post: PostCreate, db: Session):
    result = db.execute(select(models.User).where(models.User.username == post.author))
    user = result.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Author not found",
        )
    if not _verify_password(post.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid author or password",
        )
    new_post = models.Post(
        author=user,
        title=post.title,
        content=post.content,
    )
    db.add(new_post)
    db.commit()
    db.refresh(new_post)
    return new_post

#route to create a post via API documentation
@app.post("/api/Posts", response_model=PostResponse, status_code= status.HTTP_201_CREATED)
def create_post(post: PostCreate, db: Session = Depends(get_db)):
    return _create_post(post, db)


#route to get a post via API documentation
@app.get("/api/Posts/{post_id}", response_model=PostResponse)
def read_post(post_id: int, db: Session = Depends(get_db)):
    result = db.execute(select(models.Post).where(models.Post.id == post_id))
    post = result.scalars().first()
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        )
    return post

@app.delete("/api/Posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(post_id: int, db: Session = Depends(get_db)):
    result = db.execute(select(models.Post).where(models.Post.id == post_id))
    post = result.scalars().first()
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        )
    db.delete(post)
    db.commit()

@app.put("/api/Posts/{post_id}", response_model=PostResponse)
def update_full_post(
    post_id: int,
    post_data: PostUpdate,
    db: Session = Depends(get_db),
):
    result = db.execute(select(models.Post).where(models.Post.id == post_id ))
    post = result.scalars().first()
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        )
    _authorize_post_author(db, post, post_data.author, post_data.password)
    post.title = post_data.title
    post.content = post_data.content
    db.commit()
    db.refresh(post)
    return post

@app.patch("/api/Posts/{post_id}", response_model=PostResponse)
def update_partial_post(
    post_id: int,
    post_data: PostPatch,
    db: Session = Depends(get_db),
):
    result = db.execute(select(models.Post).where(models.Post.id == post_id))
    post = result.scalars().first()
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        )
    _authorize_post_author(db, post, post_data.author, post_data.password)
    if post_data.title is not None:
        post.title = post_data.title
    if post_data.content is not None:
        post.content = post_data.content
    db.commit()
    db.refresh(post)
    return post
