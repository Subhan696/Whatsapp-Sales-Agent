from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.base import get_db
from app.db.crud import create_tenant, get_tenant_by_email

router = APIRouter(prefix="/auth", tags=["auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class SignupRequest(BaseModel):
    business_name: str
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str
    tenant_id: int
    admin_api_key: str | None = None  # Returned once on signup for webhooks etc.


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(days=7)
    to_encode.update({"exp": expire})
    settings = get_settings()
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET, algorithm="HS256")
    return encoded_jwt


@router.post("/signup", response_model=AuthResponse)
async def signup(request: SignupRequest, db: AsyncSession = Depends(get_db)):
    existing = await get_tenant_by_email(db, email=request.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # Hash the password
    hashed_password = pwd_context.hash(request.password)

    # Generate an admin API key for integrations/webhooks
    new_admin_key = secrets.token_urlsafe(32)

    try:
        tenant = await create_tenant(
            db,
            name=request.business_name,
            email=request.email,
            password_hash=hashed_password,
            admin_api_key=new_admin_key,
            status="active"
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create account",
        )

    access_token = create_access_token(data={"sub": str(tenant.id)})

    return AuthResponse(
        access_token=access_token,
        token_type="bearer",
        tenant_id=tenant.id,
        admin_api_key=new_admin_key,
    )


@router.post("/login", response_model=AuthResponse)
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    tenant = await get_tenant_by_email(db, email=request.email)
    if not tenant or not tenant.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    if not pwd_context.verify(request.password, tenant.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    access_token = create_access_token(data={"sub": str(tenant.id)})

    return AuthResponse(
        access_token=access_token,
        token_type="bearer",
        tenant_id=tenant.id,
    )
