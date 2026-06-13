"""Pydantic models for the auth surface."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class User(BaseModel):
    """Public user representation (never exposes password_hash)."""
    id: str
    email: str
    disclaimer_accepted_at: str | None = None
    created_at: str
    is_admin: bool = False


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)
    # Frontend must send agree=true (checkbox) — enforced here as a backup.
    agree: bool = Field(default=False)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class AuthResponse(BaseModel):
    token: str
    user: User
