"""Connecting channel accounts. Today: LinkedIn, via OAuth ("Connect LinkedIn").

The dance, in four steps:
  1. The admin clicks Connect. The browser asks GET /linkedin/connect for LinkedIn's URL.
  2. LinkedIn shows its own consent screen. The admin signs in and approves.
  3. LinkedIn sends the browser to GET /linkedin/callback with a one-time code.
  4. We swap the code for a token, store it encrypted, and send the browser back
     to the Team page with ?linkedin=connected.

`state` travels through LinkedIn and back. It is a short-lived signed token naming the
admin who started, so nobody can forge a callback to attach their own account.
"""

from datetime import timedelta
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import runtime
from ..database import get_db, utcnow
from ..enums import Role
from ..models import ChannelConnection, User
from ..security import current_user, require
from ..services import channels, linkedin
from ..services.audit import record
from ..services.secrets_box import encrypt

router = APIRouter(prefix="/api/channels", tags=["channels"])

STATE_PURPOSE = "linkedin_connect"


@router.get("")
def channel_status(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return channels.connection_status(runtime.settings, db)


@router.get("/linkedin/connect")
def linkedin_connect(admin: User = Depends(require(Role.ADMIN))):
    s = runtime.settings
    if not (s.linkedin_client_id and s.linkedin_client_secret):
        raise HTTPException(409, "Add LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET to backend/.env first.")
    state = jwt.encode({"sub": str(admin.id), "purpose": STATE_PURPOSE, "exp": utcnow() + timedelta(minutes=10)},
                       s.jwt_secret, algorithm="HS256")
    return {"url": linkedin.authorize_url(s, state)}


@router.get("/linkedin/callback", include_in_schema=False)
def linkedin_callback(code: str = "", state: str = "", error: str = "", error_description: str = "",
                      db: Session = Depends(get_db)):
    """LinkedIn redirects the browser here. Always answers with a redirect back to the console."""
    s = runtime.settings

    def back(**params: str) -> RedirectResponse:
        return RedirectResponse(f"{s.frontend_url}/team?{urlencode(params)}", status_code=303)

    if error:   # e.g. the admin clicked Cancel on LinkedIn's screen
        return back(linkedin="error", reason=error_description or error)
    try:
        claims = jwt.decode(state, s.jwt_secret, algorithms=["HS256"])
        assert claims.get("purpose") == STATE_PURPOSE
    except (jwt.PyJWTError, AssertionError):
        return back(linkedin="error", reason="The connection request expired or was not started here. Try again.")

    admin = db.get(User, int(claims["sub"]))
    if admin is None or not admin.active or admin.role != Role.ADMIN:
        return back(linkedin="error", reason="Only an active admin can connect LinkedIn.")

    try:
        tokens = linkedin.exchange_code(s, code)
        member_urn, name = linkedin.fetch_member(tokens["access_token"])
    except linkedin.LinkedInError as exc:
        return back(linkedin="error", reason=str(exc))

    conn = db.scalar(select(ChannelConnection).where(ChannelConnection.provider == "linkedin"))
    before = channels.describe(conn) if conn else None
    if conn is None:
        conn = ChannelConnection(provider="linkedin")
        db.add(conn)
    conn.account_urn = member_urn
    conn.account_name = name
    conn.access_token_enc = encrypt(tokens["access_token"])
    conn.refresh_token_enc = encrypt(tokens.get("refresh_token", ""))
    conn.scopes = tokens.get("scope", "").replace(",", " ")
    conn.expires_at = utcnow() + timedelta(seconds=int(tokens.get("expires_in", 60 * 24 * 3600)))
    conn.connected_by_id = admin.id
    db.flush()
    record(db, admin, "channel.connected", "channel", conn.id, before=before, after=channels.describe(conn))
    db.commit()
    return back(linkedin="connected")


@router.delete("/linkedin")
def linkedin_disconnect(db: Session = Depends(get_db), admin: User = Depends(require(Role.ADMIN))):
    conn = db.scalar(select(ChannelConnection).where(ChannelConnection.provider == "linkedin"))
    if conn is None:
        raise HTTPException(404, "LinkedIn is not connected.")
    record(db, admin, "channel.disconnected", "channel", conn.id, before=channels.describe(conn))
    db.delete(conn)
    db.commit()
    return channels.connection_status(runtime.settings, db)
