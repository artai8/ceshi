"""helper functions for the bot."""
import logging
from typing import List

from telethon import events

from nb import config
from nb.config import Forward


def admin_protect(org_func):
    """Decorate to restrict non admins from accessing the bot."""

    async def wrapper_func(event):
        """Wrap the original function."""
        logging.info(f"Applying admin protection! Admins are {config.ADMINS}")
        if not config.ADMINS:
            await event.respond("⚠️ 管理员列表未配置，请先在 Web UI 中设置管理员。")
            raise events.StopPropagation
        if event.sender_id not in config.ADMINS:
            await event.respond("You are not authorized.")
            raise events.StopPropagation
        return await org_func(event)

    return wrapper_func


def get_args(text: str) -> str:
    """Return the part of message following the command."""
    splitted = text.split(" ", 1)

    if not len(splitted) == 2:
        splitted = text.split("\n", 1)
        if not len(splitted) == 2:
            return ""

    prefix, args = splitted
    args = args.strip()
    logging.info(f"Got command {prefix} with args {args}")
    return args


def display_forwards(forwards: List[Forward]) -> str:
    """Return a string that beautifully displays all current forwards."""
    if len(forwards) == 0:
        return "Currently no forwards are set"
    forward_str = "This is your configuration"
    for forward in forwards:
        sources = getattr(forward, "sources", None) or []
        forward_str = (
            forward_str
            + f"\n\n```\nsources: {sources}\ndest: {forward.dest}\n```\n"
        )

    return forward_str


def remove_source(source, forwards: List[Forward]) -> List[Forward]:
    """Remove a source from forwards."""
    for i, forward in enumerate(forwards):
        sources = list(getattr(forward, "sources", None) or [])
        if sources:
            filtered = [s for s in sources if s != source]
            if len(filtered) != len(sources):
                forward.sources = filtered
                if filtered:
                    return forwards
                del forwards[i]
                return forwards
    raise ValueError("The source does not exist")


def get_command_prefix():
    if config.is_bot is None:
        raise ValueError("config.is_bot is not set!")
    return "/" if config.is_bot else "\\."
