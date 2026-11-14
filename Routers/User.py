from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

import models
from database import get_db
from schema import Token, UserCreate, UserResponse
from services import (
    EMAIL_VERIFICATION,
    authenticate_user,
    clear_login_rate_limit,
    create_access_token,
    create_email_action_token,
    create_user_record,
    enforce_login_rate_limit,
    get_request_user,
    login_rate_limit_keys,
    record_failed_login,
)
from email_service import EmailDeliveryError, send_verification_email

router = APIRouter(prefix="/api/Users", tags=["Users"])


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    request: Request,
    user: UserCreate,
    db: Session = Depends(get_db),
):
    rate_limit_keys = login_rate_limit_keys(request, f"register:{user.email}")
    enforce_login_rate_limit(db, rate_limit_keys)
    record_failed_login(db, rate_limit_keys)
    created_user = create_user_record(user, db)
    token = create_email_action_token(created_user, EMAIL_VERIFICATION, db)
    try:
        send_verification_email(created_user.email, token)
    except EmailDeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The account was created, but the verification message could not "
                "be sent. Request a new message from the web page."
            ),
        ) from exc
    return created_user


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
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    rate_limit_keys = login_rate_limit_keys(request, form_data.username)
    enforce_login_rate_limit(db, rate_limit_keys)
    try:
        user = authenticate_user(form_data.username, form_data.password, db)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            record_failed_login(db, rate_limit_keys)
        raise
    clear_login_rate_limit(db, rate_limit_keys)
    return Token(
        access_token=create_access_token(user.id, user.auth_version),
        token_type="bearer",
    )