"""Environment-backed settings for the multi-tenant services."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    file_name = os.getenv(f"{name}_FILE", "").strip()
    if not value and file_name:
        try:
            with open(file_name, encoding="utf-8") as secret_file:
                value = secret_file.read().strip()
        except OSError as exc:
            raise RuntimeError(f"Unable to read secret file for {name}") from exc
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    mongo_uri: str
    mongo_db: str
    bot_token: str
    api_id: int
    api_hash: str
    session_encryption_key: str
    public_base_url: str
    worker_id: str
    worker_capacity: int
    lease_seconds: int
    poll_seconds: float
    admin_ids: frozenset[int]
    web_session_hours: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls._from_env("all")

    @classmethod
    def for_api(cls) -> "Settings":
        return cls._from_env("api")

    @classmethod
    def for_bot(cls) -> "Settings":
        return cls._from_env("bot")

    @classmethod
    def for_worker(cls) -> "Settings":
        return cls._from_env("worker")

    @classmethod
    def _from_env(cls, service: str) -> "Settings":
        needs_bot_token = service in {"all", "api", "bot"}
        needs_telegram_api = service in {"all", "bot", "worker"}
        needs_session_key = service in {"all", "bot", "worker"}
        needs_public_url = service in {"all", "api", "bot"}
        public_base_url = (
            _required("PUBLIC_BASE_URL").rstrip("/") if needs_public_url else ""
        )
        if public_base_url and not public_base_url.startswith("https://"):
            raise RuntimeError("PUBLIC_BASE_URL must use HTTPS")
        admin_ids = frozenset(
            int(item.strip())
            for item in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",")
            if item.strip()
        )
        return cls(
            mongo_uri=_required("MONGO_CON_STR"),
            mongo_db=os.getenv("MONGO_DB_NAME", "nb-multitenant"),
            bot_token=_required("BOT_TOKEN") if needs_bot_token else "",
            api_id=int(_required("API_ID")) if needs_telegram_api else 0,
            api_hash=_required("API_HASH") if needs_telegram_api else "",
            session_encryption_key=(
                _required("SESSION_ENCRYPTION_KEY") if needs_session_key else ""
            ),
            public_base_url=public_base_url,
            worker_id=os.getenv("WORKER_ID", f"{socket.gethostname()}-{os.getpid()}"),
            worker_capacity=max(1, int(os.getenv("WORKER_CLIENT_CAPACITY", "50"))),
            lease_seconds=max(15, int(os.getenv("WORKER_LEASE_SECONDS", "45"))),
            poll_seconds=max(1.0, float(os.getenv("WORKER_POLL_SECONDS", "5"))),
            admin_ids=admin_ids,
            web_session_hours=max(1, int(os.getenv("WEB_SESSION_HOURS", "24"))),
        )
