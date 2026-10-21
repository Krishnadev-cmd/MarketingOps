"""Sign in, who am I, and changing your own password."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import runtime
from ..database import get_db, utcnow
from ..models import User
from ..schemas import LoginIn, PasswordChangeIn, user_out
from ..security import check_login, current_user, hash_password, make_token, password_problem, verify_password
from ..services.audit import record

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(data: LoginIn, db: Session = Depends(get_db)):
    s = runtime.settings
    user = db.scalar(select(User).where(User.email == data.email.lower()))

    if user and user.locked_until and user.locked_until > utcnow():
        minutes = max(1, int((user.locked_until - utcnow()).total_seconds() // 60) + 1)
        raise HTTPException(429, f"Too many wrong passwords. Try again in {minutes} min, or ask an admin to unlock you.")

    # Same message for "no such user" and "wrong password", so emails cannot be probed.
    if not check_login(user, data.password) or not user.active:
        if user:
            user.failed_logins += 1
            if user.failed_logins >= s.max_failed_logins:
                user.locked_until = utcnow() + timedelta(minutes=s.lockout_minutes)
                user.failed_logins = 0
                record(db, None, "user.locked", "user", user.id,
                       reason=f"{s.max_failed_logins} wrong passwords in a row")
            db.commit()
        raise HTTPException(401, "Email or password is wrong.")

    user.failed_logins = 0
    user.locked_until = None
    record(db, user, "user.signed_in", "user", user.id)
    db.commit()
    return {"token": make_token(user), "user": user_out(user)}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user_out(user)


@router.post("/password")
def change_password(data: PasswordChangeIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Change your own password. Every other signed-in session ends; this one gets a fresh token."""
    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(403, "Your current password is wrong.")
    problem = password_problem(data.new_password, user.email)
    if problem:
        raise HTTPException(422, problem)
    if verify_password(data.new_password, user.password_hash):
        raise HTTPException(422, "Choose a password you have not just been using.")
    user.password_hash = hash_password(data.new_password)
    user.password_changed_at = utcnow()
    record(db, user, "user.password_changed", "user", user.id)
    db.commit()
    return {"token": make_token(user), "user": user_out(user)}


@router.get("/demo-accounts")
def demo_accounts(db: Session = Depends(get_db)):
    """For the sign-in page's quick picker. Empty unless SEED_DEMO_DATA=true. Never returns passwords."""
    if not runtime.settings.seed_demo_data:
        return []
    users = db.scalars(select(User).where(User.active.is_(True)).order_by(User.id)).all()
    return [user_out(u) for u in users]
