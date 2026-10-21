"""Encrypting channel tokens before they touch the database.

A LinkedIn access token lets anyone holding it post as you, so it is stored
encrypted (Fernet: AES-128 + HMAC from the `cryptography` package). A database
backup or a curious SQL query shows only ciphertext. The key lives in
TOKEN_ENCRYPTION_KEY, outside the database.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


class CannotDecrypt(Exception):
    """The key changed since this was stored. The fix is to reconnect the channel."""


def _fernet() -> Fernet:
    s = get_settings()
    if s.token_encryption_key:
        return Fernet(s.token_encryption_key.encode())
    # Development only (production refuses to start without a key): derive one from
    # the JWT secret. If that secret is random per run, reconnect after each restart.
    derived = base64.urlsafe_b64encode(hashlib.sha256(s.jwt_secret.encode()).digest())
    return Fernet(derived)


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode() if plain else ""


def decrypt(stored: str) -> str:
    if not stored:
        return ""
    try:
        return _fernet().decrypt(stored.encode()).decode()
    except InvalidToken as exc:
        raise CannotDecrypt("The stored token cannot be read with the current key. Reconnect the channel.") from exc
