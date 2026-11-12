from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from database import get_db
from schema import Token, UserCreate, UserResponse
from services import (
    authenticate_user,
    create_access_token,
    create_user_record,
    get_request_user,
)

router = APIRouter(prefix="/api/Users", tags=["Users"])


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(user: UserCreate, db: Session = Depends(get_db)):
    return create_user_record(user, db)


@router.get("/me", response_model=UserResponse)
def read_current_user(request: Request, db: Session = Depends(get_db)):
    user = get_request_user(request, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


@router.get("/{user_id}", response_model=UserResponse)
def read_user(user_id: int, db: Session = Depends(get_db)):
    user = db.execute(
        select(models.User).where(models.User.id == user_id)
    ).scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return user


@router.post("/token", response_model=Token)
def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = authenticate_user(form_data.username, form_data.password, db)
    return Token(
        access_token=create_access_token(user.id),
        token_type="bearer",
    )