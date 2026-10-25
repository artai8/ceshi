"""Session encryption and Telegram web authentication helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from cryptography.fernet import Fernet, InvalidToken


class SessionCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except Exception as exc:
            raise RuntimeError("SESSION_ENCRYPTION_KEY must be a Fernet key") from exc

    def encrypt(self, session: str) -> str:
        return self._fernet.encrypt(session.encode("utf8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf8")
        except InvalidToken as exc:
            raise RuntimeError("Unable to decrypt tenant session") from exc


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class ValidatedTelegramUser:
    id: int
    username: str = ""
    first_name: str = ""
    last_name: str = ""


def validate_miniapp_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 300,
) -> ValidatedTelegramUser:
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise ValueError("Missing Telegram hash")

    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf8"), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode("utf8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected):
        raise ValueError("Invalid Telegram signature")

    try:
        auth_date = int(values["auth_date"])
    except (KeyError, ValueError) as exc:
        raise ValueError("Invalid Telegram auth_date") from exc
    now = int(datetime.now(timezone.utc).timestamp())
    if auth_date > now + 30 or now - auth_date > max_age_seconds:
        raise ValueError("Expired Telegram init data")

    try:
        user = json.loads(values["user"])
        return ValidatedTelegramUser(
            id=int(user["id"]),
            username=user.get("username", ""),
            first_name=user.get("first_name", ""),
            last_name=user.get("last_name", ""),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid Telegram user data") from exc


def expiry(*, minutes: int = 0, hours: int = 0, days: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes, hours=hours, days=days)
