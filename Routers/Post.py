from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from database import get_db
from schema import PostCreate, PostPatch, PostResponse, PostUpdate
from services import (
    apply_post_metadata,
    authorize_post_owner,
    create_post_record,
    require_authenticated_user,
)
from security import require_header_csrf

router = APIRouter(prefix="/api/Posts", tags=["Posts"])


@router.post("", response_model=PostResponse, status_code=status.HTTP_201_CREATED)
def create_post(
    post: PostCreate,
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_header_csrf),
):
    user = require_authenticated_user(request, db)
    return create_post_record(post, user, db)


@router.get("/{post_id}", response_model=PostResponse)
def read_post(post_id: int, db: Session = Depends(get_db)):
    post = db.execute(
        select(models.Post).where(models.Post.id == post_id)
    ).scalars().first()
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        )
    return post


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(
    post_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_header_csrf),
) -> None:
    post = db.execute(
        select(models.Post).where(models.Post.id == post_id)
    ).scalars().first()
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        )
    user = require_authenticated_user(request, db)
    authorize_post_owner(post, user)
    db.delete(post)
    db.commit()


@router.put("/{post_id}", response_model=PostResponse)
def update_full_post(
    post_id: int,
    post_data: PostUpdate,
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_header_csrf),
):
    post = db.execute(
        select(models.Post).where(models.Post.id == post_id)
    ).scalars().first()
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        )
    user = require_authenticated_user(request, db)
    authorize_post_owner(post, user)
    post.title = post_data.title
    post.content = post_data.content
    post.summary = post_data.summary
    apply_post_metadata(
        post,
        db,
        category_name=post_data.category,
        tag_names=post_data.tags,
    )
    db.commit()
    db.refresh(post)
    return post


@router.patch("/{post_id}", response_model=PostResponse)
def update_partial_post(
    post_id: int,
    post_data: PostPatch,
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(require_header_csrf),
):
    post = db.execute(
        select(models.Post).where(models.Post.id == post_id)
    ).scalars().first()
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found",
        )
    user = require_authenticated_user(request, db)
    authorize_post_owner(post, user)
    if post_data.title is not None:
        post.title = post_data.title
    if post_data.content is not None:
        post.content = post_data.content
    if "summary" in post_data.model_fields_set:
        post.summary = post_data.summary
    if "category" in post_data.model_fields_set or "tags" in post_data.model_fields_set:
        apply_post_metadata(
            post,
            db,
            category_name=(
                post_data.category
                if "category" in post_data.model_fields_set
                else (post.category.name if post.category else None)
            ),
            tag_names=(
                post_data.tags or []
                if "tags" in post_data.model_fields_set
                else [tag.name for tag in post.tags]
            ),
        )
    db.commit()
    db.refresh(post)
    return post