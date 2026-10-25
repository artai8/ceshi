"""Read-only Telegram preflight with an opt-in send/delete probe."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession


def secret_value(name: str) -> str:
    value = os.getenv(name, "").strip()
    file_name = os.getenv(f"{name}_FILE", "").strip()
    if not value and file_name:
        value = Path(file_name).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Missing {name} or {name}_FILE")
    return value


async def run() -> None:
    api_id = int(secret_value("API_ID"))
    api_hash = secret_value("API_HASH")
    session = secret_value("TELEGRAM_TEST_SESSION")
    source = secret_value("TELEGRAM_TEST_SOURCE")
    destination = secret_value("TELEGRAM_TEST_DESTINATION")
    allow_send = os.getenv("TELEGRAM_TEST_SEND", "") == "1"

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Test Telegram session is not authorized")
        me = await client.get_me()
        source_entity = await client.get_entity(source)
        destination_entity = await client.get_entity(destination)
        await client.get_messages(source_entity, limit=1)
        permissions = await client.get_permissions(destination_entity, "me")
        is_admin = bool(
            getattr(permissions, "is_admin", False)
            or getattr(permissions, "is_creator", False)
        )
        if getattr(destination_entity, "broadcast", False):
            can_post = bool(
                getattr(permissions, "post_messages", False)
                or getattr(permissions, "is_creator", False)
            )
            if not is_admin or not can_post:
                raise RuntimeError("Test account cannot post to destination channel")
        banned = getattr(permissions, "banned_rights", None)
        if banned and getattr(banned, "send_messages", False):
            raise RuntimeError("Test account cannot send to destination")
        result = {
            "account_id": int(me.id),
            "source_id": int(source_entity.id),
            "destination_id": int(destination_entity.id),
            "destination_is_admin": is_admin,
            "send_probe": "skipped",
        }
        if allow_send:
            marker = f"NB preflight {secrets.token_hex(6)}"
            sent = await client.send_message(destination_entity, marker)
            await client.delete_messages(destination_entity, [sent.id])
            result["send_probe"] = "sent-and-deleted"
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run())
