"""Passwords, login tokens and role checks.

Passwords: PBKDF2 from the standard library, salted, 310k rounds (OWASP guidance).
Tokens: a signed JWT carrying the user id. The role is read from the database
on every request, so changing someone's role takes effect immediately.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import timedelta

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db, utcnow
from .enums import Role
from .models import User

ROUNDS = 310_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ROUNDS)
    return f"pbkdf2${ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt, digest = stored.split("$")
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(rounds))
        return hmac.compare_digest(check, base64.b64decode(digest))   # constant time
    except ValueError:
        return False


# Verified against when the email is unknown, so "no such user" takes as long as
# "wrong password" and response times cannot reveal which emails exist.
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


def check_login(user: User | None, password: str) -> bool:
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, user.password_hash)


def password_problem(password: str, email: str = "") -> str | None:
    """Length beats complexity rules (NIST 800-63B). None = acceptable."""
    if len(password) < 12:
        return "Use at least 12 characters."
    if len(password) > 128:
        return "Use at most 128 characters."
    local = email.split("@")[0].lower()
    if local and len(local) >= 4 and local in password.lower():
        return "Do not include your email name in the password."
    return None


def password_stamp(user: User) -> int:
    """Which password a token was issued under (ms since epoch of the last change, 0 = never)."""
    return int(user.password_changed_at.timestamp() * 1000) if user.password_changed_at else 0


def make_token(user: User) -> str:
    s = get_settings()
    now = utcnow()
    payload = {"sub": str(user.id), "iat": int(now.timestamp()), "exp": now + timedelta(hours=s.jwt_hours),
               "pwd": password_stamp(user)}
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


bearer = HTTPBearer(auto_error=False)


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in first.")
    try:
        payload = jwt.decode(creds.credentials, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session expired. Sign in again.")
    user = db.get(User, int(payload["sub"]))
    if user is None or not user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This account is disabled.")
    # A password change (or admin reset) ends every session issued under the old password.
    if payload.get("pwd", 0) != password_stamp(user):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your password changed. Sign in again.")
    return user


def require(*roles: Role):
    """Dependency factory: `Depends(require(Role.ADMIN))` lets only admins through."""

    def check(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            allowed = ", ".join(r.value.replace("_", " ") for r in roles)
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Only {allowed} can do this.")
        return user

    return check
