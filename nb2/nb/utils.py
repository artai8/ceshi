# nb/utils.py

import logging
import asyncio
import re
from urllib.parse import urlparse, parse_qs
import os
import sys
import platform
import tempfile
import random
from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional, Union, Tuple
import inspect

from telethon import utils as telethon_utils
from telethon.client import TelegramClient
from telethon.hints import EntityLike
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    InputMediaPhoto,
    InputMediaDocument,
    InputMediaUploadedPhoto,
    InputMediaUploadedDocument,
    InputPhoto,
    InputDocument,
    InputSingleMedia,
    InputReplyToMessage,
    MessageMediaPhoto,
    MessageMediaDocument,
    ReplyInlineMarkup,
    KeyboardButtonUrl,
    KeyboardButtonCallback,
    MessageEntityTextUrl,
    MessageEntityUrl,
    MessageService,
)
from telethon.errors.rpcerrorlist import MsgIdInvalidError
from telethon.tl.functions.messages import (
    SendMediaRequest,
    SendMultiMediaRequest,
    GetDiscussionMessageRequest,
    GetBotCallbackAnswerRequest,
)

from nb import __version__
if os.getenv("NB_MULTITENANT") == "1":
    CONFIG = None
else:
    from nb.config import CONFIG
from nb.plugin_models import STYLE_CODES

if TYPE_CHECKING:
    from nb.plugins import NbMessage


MAX_RETRIES = 2
RETRY_BASE_DELAY = 5
FORWARD_DELAY_MIN_SECONDS = 180
FORWARD_DELAY_MAX_SECONDS = 300

_DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "nb_downloads")
os.makedirs(_DOWNLOAD_DIR, exist_ok=True)

_START_LINK_CACHE_TTL_SECONDS = 120
_START_LINK_RESULT_CACHE: Dict[Tuple[str, str], Tuple[float, List[Message]]] = {}


# =====================================================================
#  平台信息
# =====================================================================

def platform_info():
    nl = "\n"
    return f"""Running nb {__version__}\
    \nPython {sys.version.replace(nl, "")}\
    \nOS {os.name}\
    \nPlatform {platform.system()} {platform.release()}\
    \n{platform.architecture()} {platform.processor()}"""


# =====================================================================
#  reply_to 兼容
# =====================================================================

def _get_reply_to_msg_id(message) -> Optional[int]:
    if hasattr(message, 'reply_to') and message.reply_to is not None:
        if hasattr(message.reply_to, 'reply_to_msg_id'):
            return message.reply_to.reply_to_msg_id
    if hasattr(message, 'reply_to_msg_id') and message.reply_to_msg_id is not None:
        return message.reply_to_msg_id
    return None


def _get_reply_to_top_id(message) -> Optional[int]:
    reply_to = getattr(message, 'reply_to', None)
    if reply_to is None:
        return None
    return getattr(reply_to, 'reply_to_top_id', None)


def _make_reply_to(msg_id: Optional[int]):
    if msg_id is None:
        return None
    try:
        return InputReplyToMessage(reply_to_msg_id=msg_id)
    except Exception:
        return msg_id


def _preview_text(text: Optional[str], limit: int = 80) -> str:
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit - 3]}..."


def describe_message(message: Optional[Message]) -> str:
    if message is None:
        return "msg=None"
    return (
        f"chat={getattr(message, 'chat_id', None)} "
        f"id={getattr(message, 'id', None)} "
        f"grouped={getattr(message, 'grouped_id', None)} "
        f"media={getattr(message, 'media', None) is not None} "
        f"spoiler={_has_spoiler(message)} "
        f"reply={getattr(message, 'is_reply', False)} "
        f"text={_preview_text(getattr(message, 'raw_text', None) or getattr(message, 'text', None), 60)!r}"
    )


def describe_nb_message(tm: Optional["NbMessage"]) -> str:
    if tm is None:
        return "tm=None"
    return (
        f"file_type={getattr(tm, 'file_type', None)} "
        f"new_file={bool(getattr(tm, 'new_file', None))} "
        f"cleanup={bool(getattr(tm, 'cleanup', False))} "
        f"reply_to={getattr(tm, 'reply_to', None)} "
        f"text={_preview_text(getattr(tm, 'text', None), 60)!r}"
    )


# =====================================================================
#  讨论区辅助
# =====================================================================

async def get_discussion_message(
    client: TelegramClient,
    channel_id: Union[int, str],
    msg_id: int,
) -> Optional[Message]:
    try:
        result = await client(GetDiscussionMessageRequest(
            peer=channel_id, msg_id=msg_id,
        ))
        if result and result.messages:
            return result.messages[0]
    except Exception as e:
        logging.warning(f"⚠️ 获取讨论消息失败 (channel={channel_id}, msg={msg_id}): {e}")
    return None


async def get_discussion_group_id(
    client: TelegramClient,
    channel_id: Union[int, str],
) -> Optional[int]:
    try:
        full = await client.get_entity(channel_id)
        if hasattr(full, 'linked_chat_id') and full.linked_chat_id:
            return full.linked_chat_id
        from telethon.tl.functions.channels import GetFullChannelRequest
        full_channel = await client(GetFullChannelRequest(channel_id))
        if hasattr(full_channel.full_chat, 'linked_chat_id'):
            return full_channel.full_chat.linked_chat_id
    except Exception as e:
        logging.warning(f"⚠️ 获取讨论组失败 (channel={channel_id}): {e}")
    return None


# =====================================================================
#  链接提取
# =====================================================================

def _extract_tme_links(text: str) -> List[str]:
    if not text:
        return []
    candidates = re.findall(r"(https?://t\.me/[^\s]+|t\.me/[^\s]+)", text)
    return [c.strip(").,;\"'") for c in candidates]


def _extract_tme_links_from_entities(message: Message) -> List[str]:
    if message is None:
        return []
    entities = getattr(message, "entities", None) or []
    text = message.raw_text or message.text or ""
    links: List[str] = []
    for ent in entities:
        if isinstance(ent, MessageEntityTextUrl):
            if ent.url:
                links.append(ent.url)
        elif isinstance(ent, MessageEntityUrl):
            if text:
                url = text[ent.offset: ent.offset + ent.length]
                if url:
                    links.append(url)
    return links


def _parse_tme_start_link(url: str) -> Optional[tuple]:
    if not url:
        return None
    if url.startswith("t.me/"):
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    path = parsed.path.lstrip("/")
    if not path:
        return None
    bot_username = path.split("/")[0]
    qs = parse_qs(parsed.query or "")
    start_param = qs.get("start", [None])[0]
    if not start_param:
        return None
    return bot_username, start_param


def _extract_start_links_from_markup(reply_markup, forward=None) -> List[tuple]:
    if reply_markup is None or not isinstance(reply_markup, ReplyInlineMarkup):
        return []
    blacklist = _get_tme_link_blacklist(forward)
    found = []
    for row in reply_markup.rows:
        for button in row.buttons:
            if isinstance(button, KeyboardButtonUrl):
                url = button.url or ""
                if blacklist:
                    url_lower = url.lower()
                    if any(token in url_lower for token in blacklist):
                        continue
                parsed = _parse_tme_start_link(url)
                if parsed:
                    found.append(parsed)
    return found


def _extract_bot_usernames(text: str) -> List[str]:
    if not text:
        return []
    matches = re.findall(r"@([A-Za-z0-9_]{5,})", text)
    return [name for name in matches if name.lower().endswith("bot")]


def _parse_lines(raw: str) -> List[str]:
    if not raw:
        return []
    lines = [line.strip() for line in raw.replace("\r", "\n").split("\n")]
    return [line for line in lines if line]


def _parse_start_links_from_text(text: str, forward=None) -> List[tuple]:
    links = _filter_tme_links(_extract_tme_links(text), forward)
    return [p for p in ((_parse_tme_start_link(l)) for l in links) if p]


def _parse_start_links_from_entities(message: Message, forward=None) -> List[tuple]:
    links = _filter_tme_links(_extract_tme_links_from_entities(message), forward)
    return [p for p in ((_parse_tme_start_link(l)) for l in links) if p]


def get_random_forward_delay() -> int:
    return random.randint(FORWARD_DELAY_MIN_SECONDS, FORWARD_DELAY_MAX_SECONDS)


# =====================================================================
#  评论区收集
# =====================================================================

async def _collect_discussion_comments(
    client: TelegramClient,
    channel_id: Union[int, str],
    post_id: int,
) -> tuple:
    disc_msg = await get_discussion_message(client, channel_id, post_id)
    if disc_msg is None:
        return None, [], None
    comments: List[Message] = []
    counts = {}
    keyword_hint = None
    try:
        async for comment in client.iter_messages(
            disc_msg.chat_id, reply_to=disc_msg.id,
            reverse=True, limit=CONFIG.bot_media.recent_limit,
        ):
            if isinstance(comment, MessageService):
                continue
            comments.append(comment)
            text = (comment.raw_text or comment.text or "").strip()
            text = _trim_keyword(text)
            if text:
                counts[text] = counts.get(text, 0) + 1
                if counts[text] >= 5:
                    keyword_hint = text
                    break
    except MsgIdInvalidError as e:
        logging.warning(f"⚠️ 讨论区消息 ID 无效 post={post_id}: {e}")
        return disc_msg, [], None
    return disc_msg, comments, keyword_hint


def _find_common_comment_keyword(comments: List[Message]) -> Optional[str]:
    counts = {}
    order = []
    for comment in comments:
        text = (comment.raw_text or comment.text or "").strip()
        text = _trim_keyword(text)
        if not text:
            continue
        if text not in counts:
            counts[text] = 0
            order.append(text)
        counts[text] += 1
    best = None
    best_count = 0
    best_idx = None
    for idx, text in enumerate(order):
        count = counts.get(text, 0)
        if count < 3:
            continue
        if count > best_count or (count == best_count and (best_idx is None or idx < best_idx)):
            best = text
            best_count = count
            best_idx = idx
    return best


async def _collect_start_links_from_keyword_reply(
    client: TelegramClient, disc_msg: Message,
    keyword: str, forward=None,
    send_keyword: bool = True,
) -> List[tuple]:
    latest = await client.get_messages(disc_msg.chat_id, limit=1)
    last_id = latest[0].id if latest else 0
    logging.info(
        f"🔎 评论关键词链路开始 discussion={describe_message(disc_msg)} "
        f"keyword={keyword!r} send_keyword={send_keyword} snapshot_last_id={last_id}"
    )
    request_msg_id = None
    if send_keyword:
        try:
            sent = await client.send_message(disc_msg.chat_id, keyword, reply_to=disc_msg.id)
            request_msg_id = getattr(sent, 'id', None)
            logging.info(
                f"💬 评论关键词已发送 discussion_chat={disc_msg.chat_id} "
                f"reply_to={disc_msg.id} keyword={keyword!r} request_msg_id={request_msg_id}"
            )
        except Exception as e:
            logging.warning(f"⚠️ 评论区关键词发送失败: {e}")
            return []
    links = []
    wait_timeout = getattr(CONFIG.bot_media, "wait_timeout", 5)
    total_timeout = max(wait_timeout * 2, 8)
    start = asyncio.get_running_loop().time()
    poll_round = 0
    while True:
        poll_round += 1
        responses = await _collect_new_messages(
            client, disc_msg.chat_id, last_id, wait_timeout
        )
        logging.info(
            f"🔎 评论关键词轮询 round={poll_round} discussion_chat={disc_msg.chat_id} "
            f"last_id={last_id} responses={len(responses)}"
        )
        if responses:
            last_id = max(m.id for m in responses)
            for msg in responses:
                logging.info(f"↩️ 评论关键词收到回复 {describe_message(msg)}")

        matched_responses = []
        for msg in responses:
            if request_msg_id is None:
                matched_responses.append(msg)
                continue

            reply_to_msg_id = _get_reply_to_msg_id(msg)
            text = (msg.raw_text or msg.text or "").lower()
            normalized_keyword = keyword.lower()
            if reply_to_msg_id == request_msg_id or normalized_keyword in text:
                matched_responses.append(msg)
            else:
                logging.info(
                    f"↪️ 忽略无关评论回复 msg={msg.id} reply_to={reply_to_msg_id} "
                    f"keyword={keyword!r} text={_preview_text(msg.raw_text or msg.text or '', 60)!r}"
                )

        for msg in matched_responses:
            button_links = _extract_start_links_from_markup(msg.reply_markup, forward)
            if button_links:
                logging.info(
                    f"🔗 从评论按钮提取到 start 链接 count={len(button_links)} "
                    f"reply_msg={msg.id}"
                )
                return button_links

        for msg in matched_responses:
            links.extend(_parse_start_links_from_text(msg.raw_text or msg.text or "", forward))
            links.extend(_parse_start_links_from_entities(msg, forward))
        if links:
            logging.info(
                f"🔗 从评论文本提取到 start 链接 count={len(links)} discussion_chat={disc_msg.chat_id}"
            )
            return links
        if asyncio.get_running_loop().time() - start >= total_timeout:
            logging.info(
                f"⌛ 评论关键词等待超时 discussion_chat={disc_msg.chat_id} "
                f"keyword={keyword!r} total_timeout={total_timeout}s"
            )
            break
    return links


async def _collect_from_start_links(
    client: TelegramClient, links: List[tuple], forward=None,
) -> List[Message]:
    if not links:
        logging.info("🤖 start 链接为空，跳过 bot 拉取")
        return []
    deduped_links = []
    seen_links = set()
    for bot_username, start_param in links:
        cache_key = ((bot_username or "").lower(), start_param or "")
        if cache_key in seen_links:
            continue
        seen_links.add(cache_key)
        deduped_links.append((bot_username, start_param))

    logging.info(
        f"🤖 准备从 start 链接拉取资源 count={len(deduped_links)} links={deduped_links}"
    )
    now = asyncio.get_running_loop().time()
    for bot_username, start_param in deduped_links:
        cache_key = ((bot_username or "").lower(), start_param or "")
        cached = _START_LINK_RESULT_CACHE.get(cache_key)
        if cached is not None:
            cached_at, cached_items = cached
            if now - cached_at <= _START_LINK_CACHE_TTL_SECONDS and cached_items:
                logging.info(
                    f"🤖 复用缓存的 bot 资源 @{bot_username} start={start_param} items={len(cached_items)}"
                )
                return list(cached_items)
            _START_LINK_RESULT_CACHE.pop(cache_key, None)

        try:
            logging.info(f"🤖 开始尝试 bot 拉取 @{bot_username} start={start_param}")
            items = await _start_bot_and_collect_album(
                client, bot_username, start_param, forward=forward
            )
            if items:
                _START_LINK_RESULT_CACHE[cache_key] = (
                    asyncio.get_running_loop().time(),
                    list(items),
                )
                logging.info(
                    f"🤖 bot 拉取成功 @{bot_username} items={len(items)} first={describe_message(items[0])}"
                )
                return items
            logging.info(f"🤖 bot 拉取无资源 @{bot_username} start={start_param}")
        except Exception as e:
            logging.warning(f"⚠️ bot 媒体拉取失败 ({bot_username}): {e}")
    return []


# =====================================================================
#  配置辅助
# =====================================================================

def _trim_keyword(value: str) -> str:
    if not value:
        return value
    return value.strip().strip(" \"'""''()（）[]【】{}<>《》")


def _get_bot_media_value(forward, name: str, default: str = "") -> str:
    if forward is not None:
        value = getattr(forward, name, "")
        if isinstance(value, str) and value.strip():
            return value
    value = getattr(CONFIG.bot_media, name, default)
    return value if isinstance(value, str) else default


def _get_bot_media_list(forward, forward_field: str, config_field: str) -> List[str]:
    raw = _get_bot_media_value(forward, forward_field, "")
    items = _parse_lines(raw)
    if not items:
        items = _parse_lines(getattr(CONFIG.bot_media, config_field, ""))
    return [item.strip().lower() for item in items if item and item.strip()]


def _get_tme_link_blacklist(forward) -> List[str]:
    return _get_bot_media_list(forward, "bot_media_tme_link_blacklist_raw", "tme_link_blacklist_raw")


def _filter_tme_links(links: List[str], forward) -> List[str]:
    blacklist = _get_tme_link_blacklist(forward)
    if not blacklist:
        return links
    return [l for l in links if not any(t in (l or "").lower() for t in blacklist)]


def _get_pagination_ignore_keywords(forward) -> List[str]:
    return _get_bot_media_list(forward, "bot_media_pagination_ignore_keywords_raw", "pagination_ignore_keywords_raw")


def _extract_comment_keyword(text: str, forward=None) -> Optional[str]:
    if not text:
        return None
    prefixes = _parse_lines(_get_bot_media_value(forward, "comment_keyword_prefixes_raw"))
    suffixes = _parse_lines(_get_bot_media_value(forward, "comment_keyword_suffixes_raw"))
    if not prefixes or not suffixes:
        return None
    for prefix in prefixes:
        start = text.find(prefix)
        if start == -1:
            continue
        start_idx = start + len(prefix)
        end_candidates = [text.find(suffix, start_idx) for suffix in suffixes]
        end_candidates = [e for e in end_candidates if e != -1]
        if not end_candidates:
            continue
        keyword = _trim_keyword(text[start_idx:min(end_candidates)])
        if keyword:
            return keyword
    return None


def _find_next_callback_button(reply_markup, forward=None) -> Optional[KeyboardButtonCallback]:
    if reply_markup is None or not isinstance(reply_markup, ReplyInlineMarkup):
        return None
    mode = _get_bot_media_value(forward, "bot_media_pagination_mode", "")
    if not mode:
        mode = getattr(CONFIG.bot_media, "pagination_mode", "auto")
    next_keywords = [
        "next", "more", "next page", "nextpage", "continue", "remaining", "send remaining",
        "下一页", "下页", "继续", "更多", "继续发送", "发送剩余", "剩余", "查看更多", "下一个", "翻页", "➡", ">",
    ]
    get_all_keywords = [
        "get all", "getall", "all", "all files", "fetch all", "download all",
        "获取全部", "全部获取", "一键获取", "获取所有", "查看全部", "全部发送", "一键发送",
    ]
    custom_keywords = _parse_lines(_get_bot_media_value(forward, "bot_media_pagination_keywords_raw"))
    if not custom_keywords:
        custom_keywords = _parse_lines(CONFIG.bot_media.pagination_keywords_raw)
    keywords = [] if mode == "any" else next_keywords + get_all_keywords + custom_keywords
    ignore_keywords = _get_pagination_ignore_keywords(forward)
    for row in reply_markup.rows:
        for button in row.buttons:
            if isinstance(button, KeyboardButtonCallback):
                text = (button.text or "").strip().lower()
                compact = text.replace(" ", "")
                if ignore_keywords and any(k in text or k in compact for k in ignore_keywords):
                    continue
                if mode == "any":
                    return button
                if any(k in text or k in compact for k in keywords):
                    return button
    return None


# =====================================================================
#  自动评论
# =====================================================================

async def _auto_comment_keyword(
    client: TelegramClient, channel_id: Union[int, str],
    post_id: int, keyword: str,
) -> bool:
    if CONFIG.login.user_type == 0:
        return False
    try:
        await client.send_message(channel_id, keyword, comment_to=post_id)
        return True
    except Exception as e:
        logging.warning(f"⚠️ 评论区触发失败: {e}")
        return False


async def trigger_comment_keyword_and_resolve_bot_media(
    client: TelegramClient,
    channel_id: Union[int, str],
    post_id: int,
    keyword: str,
    forward=None,
) -> List[Message]:
    keyword = _trim_keyword(keyword or "")
    if not keyword or CONFIG.login.user_type == 0:
        logging.info(
            f"🤖 评论关键词触发跳过 channel={channel_id} post={post_id} "
            f"keyword={keyword!r} user_type={CONFIG.login.user_type}"
        )
        return []

    logging.info(
        f"🤖 评论关键词触发开始 channel={channel_id} post={post_id} keyword={keyword!r}"
    )

    # 必须先获取讨论消息，再发送关键词
    # 保证 _collect_start_links_from_keyword_reply 能在发送前快照 last_id
    disc_msg = await get_discussion_message(client, channel_id, post_id)
    if disc_msg is None:
        logging.info(
            f"🤖 未找到讨论消息 channel={channel_id} post={post_id}"
        )
        return []

    # send_keyword=True: 内部先快照 last_id → 发送关键词 → 等待机器人回复
    keyword_links = await _collect_start_links_from_keyword_reply(
        client, disc_msg, keyword, forward, send_keyword=True
    )
    if not keyword_links:
        logging.info(
            f"🤖 评论区未等到 start 链接 post={post_id} keyword={keyword!r}"
        )
        return []

    logging.info(
        f"🤖 评论关键词已拿到 start 链接 post={post_id} count={len(keyword_links)}"
    )

    collected = await _collect_from_start_links(client, keyword_links, forward)
    if collected:
        logging.info(f"🤖 评论关键词拉取到资源 post={post_id} count={len(collected)}")
        return _filter_bot_media_by_blacklist(collected)
    logging.info(f"🤖 评论关键词未拉取到任何资源 post={post_id}")
    return []


# =====================================================================
#  消息轮询
# =====================================================================

async def _collect_new_messages(
    client: TelegramClient, peer, min_id: int, timeout: float,
) -> List[Message]:
    start = asyncio.get_running_loop().time()
    seen = set()
    collected: List[Message] = []
    current_min_id = min_id
    round_no = 0
    logging.info(f"📥 开始轮询新消息 peer={peer} min_id={min_id} timeout={timeout}s")
    while True:
        round_no += 1
        new_found = False
        async for msg in client.iter_messages(peer, min_id=current_min_id, reverse=True):
            if msg.id in seen:
                continue
            seen.add(msg.id)
            collected.append(msg)
            new_found = True
            if msg.id > current_min_id:
                current_min_id = msg.id
        if collected:
            logging.info(
                f"📥 新消息轮询 round={round_no} peer={peer} accumulated={len(collected)} "
                f"current_min_id={current_min_id}"
            )
        if collected and not new_found:
            break
        if asyncio.get_running_loop().time() - start >= timeout:
            logging.info(
                f"⌛ 新消息轮询超时 peer={peer} elapsed={asyncio.get_running_loop().time() - start:.1f}s "
                f"collected={len(collected)}"
            )
            break
        await asyncio.sleep(CONFIG.bot_media.poll_interval)
    return collected


# =====================================================================
#  Bot 媒体组收集
# =====================================================================

async def _get_grouped_messages_from_bot(
    client: TelegramClient, bot, grouped_id: int,
) -> List[Message]:
    result = []
    scan_limit = max(getattr(CONFIG.bot_media, 'recent_limit', 50), 100)
    async for msg in client.iter_messages(bot, limit=scan_limit):
        if msg.grouped_id == grouped_id:
            result.append(msg)
    result.sort(key=lambda m: m.id)
    return result


async def _start_bot_and_collect_album(
    client: TelegramClient, bot_username: str, start_param: str,
    max_pages: Optional[int] = None, wait_timeout: Optional[float] = None,
    forward=None,
) -> List[Message]:
    if max_pages is None:
        max_pages = CONFIG.bot_media.max_pages
    if not CONFIG.bot_media.enable_pagination:
        max_pages = 0
    if wait_timeout is None:
        wait_timeout = CONFIG.bot_media.wait_timeout
    logging.info(f"🤖 bot 拉取: @{bot_username} start={start_param}")
    bot = await client.get_entity(bot_username)
    latest = await client.get_messages(bot, limit=1)
    last_id = latest[0].id if latest else 0
    logging.info(
        f"🤖 bot 拉取初始化 @{bot_username} last_id={last_id} max_pages={max_pages} wait_timeout={wait_timeout}"
    )
    await client.send_message(bot, f"/start {start_param}")
    collected: List[Message] = []
    seen_grouped = set()
    seen_ids = set()
    pages = 0
    while pages <= max_pages:
        new_msgs = await _collect_new_messages(client, bot, last_id, wait_timeout)
        logging.info(
            f"🤖 bot 拉取分页 @{bot_username} page={pages} new_msgs={len(new_msgs)} last_id_before={last_id}"
        )
        if not new_msgs:
            break
        last_id = max(m.id for m in new_msgs)
        logging.info(f"🤖 bot 拉取更新 last_id @{bot_username} -> {last_id}")
        for msg in new_msgs:
            if msg.grouped_id:
                if msg.grouped_id in seen_grouped:
                    if msg.id not in seen_ids and _msg_has_media(msg):
                        collected.append(msg)
                        seen_ids.add(msg.id)
                    continue
                grouped = await _get_grouped_messages_from_bot(client, bot, msg.grouped_id)
                logging.info(
                    f"🤖 bot 拉取发现媒体组 @{bot_username} grouped_id={msg.grouped_id} size={len(grouped)}"
                )
                for gmsg in grouped:
                    if _msg_has_media(gmsg) and gmsg.id not in seen_ids:
                        collected.append(gmsg)
                        seen_ids.add(gmsg.id)
                seen_grouped.add(msg.grouped_id)
            else:
                if _msg_has_media(msg) and msg.id not in seen_ids:
                    collected.append(msg)
                    seen_ids.add(msg.id)
        next_btn = None
        next_msg = None
        for msg in reversed(new_msgs):
            next_btn = _find_next_callback_button(msg.reply_markup, forward)
            if next_btn:
                next_msg = msg
                break
        if next_btn and next_msg:
            try:
                logging.info(
                    f"🤖 bot 拉取点击分页 @{bot_username} msg_id={next_msg.id} text={getattr(next_btn, 'text', '')!r}"
                )
                await client(GetBotCallbackAnswerRequest(
                    peer=bot, msg_id=next_msg.id, data=next_btn.data
                ))
                pages += 1
                continue
            except Exception:
                logging.info(f"🤖 bot 拉取分页点击失败 @{bot_username}，停止继续翻页")
                break
        break
    collected.sort(key=lambda m: m.id)
    logging.info(f"🤖 bot 拉取完成: @{bot_username} collected={len(collected)}")
    return collected


def _msg_has_media(msg: Message) -> bool:
    if msg.photo or msg.video or msg.gif or msg.document:
        return True
    web_page = getattr(msg, 'web_page', None)
    if web_page is not None:
        if getattr(web_page, 'document', None) or getattr(web_page, 'photo', None):
            return True
    return False


def _guess_message_file_type(message: Message) -> str:
    for ft in ["photo", "video", "gif", "audio", "document", "sticker", "contact"]:
        if getattr(message, ft, None):
            return ft
    return "nofile"


def _message_hits_filter_blacklist(message: Message) -> bool:
    filters = getattr(CONFIG.plugins, "filter", None)
    if not filters or not getattr(filters, "check", False):
        return False

    sender = str(getattr(message, "sender_id", ""))
    users = getattr(filters, "users", None)
    if users is not None and sender in (users.blacklist or []):
        return True

    flist = getattr(filters, "files", None)
    if flist is not None:
        file_type = _guess_message_file_type(message)
        bl_values = [f.value if hasattr(f, "value") else f for f in (flist.blacklist or [])]
        if file_type in bl_values:
            return True

    textf = getattr(filters, "text", None)
    if textf is not None:
        text = message.raw_text or message.text or ""
        blacklist = list(textf.blacklist or [])
        if not textf.case_sensitive and not textf.regex:
            text = text.lower()
            blacklist = [item.lower() for item in blacklist]
        for forbidden in blacklist:
            if match(forbidden, text, textf.regex):
                return True

    return False


def _filter_bot_media_by_blacklist(messages: List[Message]) -> List[Message]:
    return [m for m in (messages or []) if not _message_hits_filter_blacklist(m)]


# =====================================================================
#  bot 媒体解析入口
# =====================================================================

async def resolve_bot_media_from_message(
    client: TelegramClient, message: Message, forward=None,
) -> List[Message]:
    if CONFIG.login.user_type == 0:
        return []
    if _message_hits_filter_blacklist(message):
        return []
    raw_text = message.raw_text or message.text or ""
    collected = await _collect_from_start_links(
        client, _parse_start_links_from_text(raw_text, forward), forward,
    )
    if collected:
        return _filter_bot_media_by_blacklist(collected)
    disc_msg = None
    comments: List[Message] = []
    if getattr(message, "post", False):
        disc_msg, comments, keyword_hint = await _collect_discussion_comments(client, message.chat_id, message.id)
    if comments:
        if keyword_hint and disc_msg is not None:
            keyword_links = await _collect_start_links_from_keyword_reply(client, disc_msg, keyword_hint, forward)
            collected = await _collect_from_start_links(client, keyword_links, forward)
            if collected:
                return _filter_bot_media_by_blacklist(collected)
        for src_fn in [
            lambda c: _parse_start_links_from_text(c.raw_text or c.text or "", forward),
            lambda c: _extract_start_links_from_markup(c.reply_markup, forward),
            lambda c: _parse_start_links_from_entities(c, forward),
        ]:
            all_links = []
            for comment in comments:
                all_links.extend(src_fn(comment))
            collected = await _collect_from_start_links(client, all_links, forward)
            if collected:
                return _filter_bot_media_by_blacklist(collected)
        comment_keyword_enabled = getattr(CONFIG.bot_media, "comment_keyword_from_comments_enabled", True)
        if forward is not None and forward.comment_keyword_from_comments_enabled is not None:
            comment_keyword_enabled = forward.comment_keyword_from_comments_enabled
        if comment_keyword_enabled and disc_msg is not None:
            keyword = _find_common_comment_keyword(comments)
            if keyword:
                keyword_links = await _collect_start_links_from_keyword_reply(client, disc_msg, keyword, forward)
                collected = await _collect_from_start_links(client, keyword_links, forward)
                if collected:
                    return _filter_bot_media_by_blacklist(collected)
    tail_links = []
    tail_links.extend(_parse_start_links_from_entities(message, forward))
    tail_links.extend(_extract_start_links_from_markup(message.reply_markup, forward))
    collected = await _collect_from_start_links(client, tail_links, forward)
    if collected:
        return _filter_bot_media_by_blacklist(collected)
    keyword_trigger_enabled = CONFIG.bot_media.enable_keyword_trigger
    if forward is not None and forward.bot_media_keyword_trigger_enabled is not None:
        keyword_trigger_enabled = forward.bot_media_keyword_trigger_enabled
    if not keyword_trigger_enabled:
        return []
    bot_names = _extract_bot_usernames(raw_text)
    if not bot_names:
        return []
    keyword = raw_text.strip()
    if not keyword:
        return []
    collected_kw: List[Message] = []
    for bot_username in bot_names[:1]:
        try:
            bot = await client.get_entity(bot_username)
            latest = await client.get_messages(bot, limit=1)
            last_id = latest[0].id if latest else 0
            await client.send_message(bot, keyword)
            responses = await _collect_new_messages(client, bot, last_id, CONFIG.bot_media.wait_timeout)
            for msg in responses:
                for link in _parse_start_links_from_text(msg.raw_text or msg.text or "", forward):
                    items = await _start_bot_and_collect_album(client, link[0], link[1], forward=forward)
                    if items:
                        collected_kw.extend(items)
            if collected_kw:
                break
        except Exception as e:
            logging.warning(f"⚠️ bot 关键字请求失败 ({bot_username}): {e}")
    if collected_kw:
        return _filter_bot_media_by_blacklist(collected_kw)
    return collected_kw


# =====================================================================
#  错误检测
# =====================================================================

def _is_flood_wait(e: Exception) -> bool:
    return "FLOOD_WAIT" in str(e).upper() or "flood" in str(e).lower()


def _is_disconnected_error(e: Exception) -> bool:
    text = str(e).lower()
    return "cannot send requests while disconnected" in text


async def _handle_flood_wait(e: Exception) -> int:
    wait_match = re.search(r'(\d+)', str(e))
    wait_sec = int(wait_match.group()) if wait_match else 30
    logging.critical(f"⛔ FloodWait: 等待 {wait_sec + 10} 秒")
    await asyncio.sleep(wait_sec + 10)
    return wait_sec


def _is_protected_chat_error(err: Exception) -> bool:
    text = str(err).lower()
    return "protected chat" in text or "you can't forward messages from a protected chat" in text


def _is_file_reference_error(err: Exception) -> bool:
    return "FILE_REFERENCE" in str(err).upper()


# =====================================================================
#  Spoiler 检测
# =====================================================================

_SPOILER_MARKS: dict = {}  # id(message) -> weakref-like tracking
_SPOILER_MAX_SIZE = 10000


def mark_spoiler(message: Message) -> None:
    """标记消息为 spoiler。"""
    # 淘汰旧条目防止无限增长
    if len(_SPOILER_MARKS) > _SPOILER_MAX_SIZE:
        to_remove = list(_SPOILER_MARKS.keys())[:_SPOILER_MAX_SIZE // 2]
        for k in to_remove:
            _SPOILER_MARKS.pop(k, None)
    _SPOILER_MARKS[id(message)] = True
    try:
        setattr(message, "_nb_spoiler", True)
    except Exception:
        pass
    try:
        if getattr(message, "media", None) is not None:
            setattr(message.media, "spoiler", True)
    except Exception:
        pass


def _has_spoiler(message: Message) -> bool:
    if not message or not message.media:
        return False
    if id(message) in _SPOILER_MARKS:
        return True
    if getattr(message, '_nb_spoiler', False):
        return True
    return getattr(message.media, 'spoiler', False)


def _any_has_spoiler(messages: List[Message]) -> bool:
    return any(_has_spoiler(m) for m in messages)


def _join_message_texts(messages: List[Message]) -> str:
    return "\n\n".join(
        [
            text.strip()
            for msg in messages or []
            for text in [msg.raw_text or msg.text or ""]
            if text and text.strip()
        ]
    )


def _join_tm_texts(tms: Optional[List["NbMessage"]]) -> Optional[str]:
    if tms is None:
        return None
    return "\n\n".join(
        [tm.text.strip() for tm in tms if tm and tm.text and tm.text.strip()]
    )


def _can_forward_grouped_messages_directly(
    grouped_messages: List[Message],
    grouped_caption: Optional[str] = None,
    grouped_tms: Optional[List["NbMessage"]] = None,
) -> bool:
    if not grouped_messages:
        return False
    if _any_has_spoiler(grouped_messages):
        return False
    grouped_ids = {msg.grouped_id for msg in grouped_messages if getattr(msg, "grouped_id", None)}
    if len(grouped_messages) > 1 and len(grouped_ids) != 1:
        return False
    desired_caption = grouped_caption
    if desired_caption is None:
        desired_caption = _join_tm_texts(grouped_tms)
    if desired_caption is not None:
        return desired_caption == _join_message_texts(grouped_messages)
    return True


# =====================================================================
#  文本 entities 解析
# =====================================================================

def _parse_caption_entities(text: str, parse_mode: str = "md"):
    if not text:
        return "", []
    try:
        from telethon.extensions import markdown, html
        if parse_mode == "html":
            return html.parse(text)
        else:
            return markdown.parse(text)
    except Exception:
        return text, []


# =====================================================================
#  安全下载媒体
# =====================================================================

async def _safe_download_media(message: Message, client: TelegramClient = None) -> Optional[str]:
    """
    下载消息媒体到临时目录。
    优先使用 message 自身的 client，如果不可用则使用传入的 client。
    """
    try:
        # 优先用消息自带 client
        msg_client = getattr(message, '_client', None) or getattr(message, 'client', None)
        dl_client = msg_client or client
        if dl_client is None:
            logging.warning("⚠️ 无可用 client 下载媒体")
            return None
        file = await dl_client.download_media(message, file=_DOWNLOAD_DIR)
        return file
    except Exception as e:
        logging.warning(f"⚠️ 媒体下载失败: {e}")
        return None


# =====================================================================
#  检测 send_file spoiler 支持
# =====================================================================

def _send_file_supports_spoiler() -> bool:
    try:
        sig = inspect.signature(TelegramClient.send_file)
        return 'has_spoiler' in sig.parameters
    except Exception:
        return False


_SUPPORTS_SPOILER = _send_file_supports_spoiler()


# =====================================================================
#  构建 InputMedia
# =====================================================================

def _build_input_media(media, spoiler: bool = False):
    if isinstance(media, MessageMediaPhoto) and media.photo:
        photo = media.photo
        return InputMediaPhoto(
            id=InputPhoto(
                id=photo.id, access_hash=photo.access_hash,
                file_reference=photo.file_reference,
            ),
            spoiler=spoiler,
        )
    elif isinstance(media, MessageMediaDocument) and media.document:
        doc = media.document
        return InputMediaDocument(
            id=InputDocument(
                id=doc.id, access_hash=doc.access_hash,
                file_reference=doc.file_reference,
            ),
            spoiler=spoiler,
        )
    return None


def _extract_sent_messages(result) -> List[Message]:
    sent = []
    if hasattr(result, 'updates'):
        for update in result.updates:
            if hasattr(update, 'message'):
                sent.append(update.message)
    elif result is not None:
        if isinstance(result, list):
            sent.extend([msg for msg in result if msg is not None])
        else:
            sent.append(result)
    return sent


async def _build_uploaded_input_media(
    client: TelegramClient,
    file_path: str,
    message: Message,
    spoiler: bool = False,
):
    uploaded_file = await client.upload_file(file_path)
    media = getattr(message, 'media', None)

    if isinstance(media, MessageMediaPhoto):
        return InputMediaUploadedPhoto(file=uploaded_file, spoiler=spoiler)

    if isinstance(media, MessageMediaDocument):
        is_video = bool(message.video or message.gif)
        voice = getattr(message.document, 'voice', False) if message.document else False
        video_note = bool(getattr(message, 'video_note', None))
        attributes, mime_type = telethon_utils.get_attributes(
            file_path,
            voice_note=voice,
            video_note=video_note,
            supports_streaming=is_video,
        )
        return InputMediaUploadedDocument(
            file=uploaded_file,
            mime_type=mime_type,
            attributes=attributes,
            force_file=False,
            spoiler=spoiler,
        )

    return None


async def _send_uploaded_single_media(
    client: TelegramClient,
    recipient: EntityLike,
    file_path: str,
    message: Message,
    caption: Optional[str],
    reply_to: Optional[int],
    spoiler: bool = False,
) -> Optional[Message]:
    peer = await client.get_input_entity(recipient)
    input_media = await _build_uploaded_input_media(
        client, file_path, message, spoiler=spoiler,
    )
    if input_media is None:
        return None

    msg_text, msg_entities = _parse_caption_entities(caption or "")
    kwargs = {
        'peer': peer,
        'media': input_media,
        'message': msg_text,
        'random_id': random.randrange(-2 ** 63, 2 ** 63),
    }
    if msg_entities:
        kwargs['entities'] = msg_entities
    if reply_to is not None:
        kwargs['reply_to'] = _make_reply_to(reply_to)

    result = await client(SendMediaRequest(**kwargs))
    sent = _extract_sent_messages(result)
    return sent[0] if sent else result


async def _send_uploaded_album_media(
    client: TelegramClient,
    recipient: EntityLike,
    file_items: List[Tuple[Message, str]],
    caption: Optional[str],
    reply_to: Optional[int],
    preserve_spoiler: bool = False,
) -> List[Message]:
    peer = await client.get_input_entity(recipient)
    multi_media = []

    for message, file_path in file_items:
        is_spoiler = preserve_spoiler and _has_spoiler(message)
        input_media = await _build_uploaded_input_media(
            client, file_path, message, spoiler=is_spoiler,
        )
        if input_media is None:
            continue
        if len(multi_media) == 0 and caption:
            msg_text, msg_entities = _parse_caption_entities(caption)
        else:
            msg_text = ""
            msg_entities = []
        multi_media.append(
            InputSingleMedia(
                media=input_media,
                random_id=random.randrange(-2 ** 63, 2 ** 63),
                message=msg_text,
                entities=msg_entities if msg_entities else [],
            )
        )

    if not multi_media:
        return []

    kwargs = {'peer': peer, 'multi_media': multi_media}
    if reply_to is not None:
        kwargs['reply_to'] = _make_reply_to(reply_to)

    result = await client(SendMultiMediaRequest(**kwargs))
    return _extract_sent_messages(result)


# =====================================================================
#  核心修复：下载重传（单条）—— 正确传 caption 和 spoiler
# =====================================================================

async def _send_single_by_upload(
    client: TelegramClient,
    recipient: EntityLike,
    message: Message,
    caption: Optional[str],
    reply_to: Optional[int],
    preserve_spoiler: bool = False,
) -> Union[Message, None]:
    """
    下载消息媒体后重新上传。
    关键：使用文件路径而非 MessageMedia 对象，确保 caption 生效。
    """
    file = await _safe_download_media(message, client)
    if not file:
        # 无媒体可下载，发送纯文本
        if caption:
            return await client.send_message(
                recipient, caption, reply_to=reply_to, parse_mode="md"
            )
        return None

    is_spoiler = preserve_spoiler and _has_spoiler(message)
    # 判断是否为视频（需要 supports_streaming）
    is_video = bool(message.video or message.gif)
    # 判断是否为语音/圆形视频等需要特殊属性的
    voice = getattr(message.document, 'voice', False) if message.document else False
    video_note = bool(getattr(message, 'video_note', None))

    try:
        kwargs = {
            'entity': recipient,
            'file': file,
            'caption': caption or "",
            'reply_to': reply_to,
            'supports_streaming': is_video,
            'force_document': False,
            'allow_cache': False,
            'parse_mode': "md",
            'voice_note': voice,
            'video_note': video_note,
        }
        if is_spoiler and _SUPPORTS_SPOILER:
            kwargs['has_spoiler'] = True

        result = await client.send_file(**kwargs)
        logging.info(f"✅ 下载重传成功 (spoiler={is_spoiler})")
        return result
    except Exception as e:
        logging.error(f"❌ 下载重传失败: {e}")
        # 最后兜底：发送纯文本
        if caption:
            try:
                return await client.send_message(
                    recipient, caption, reply_to=reply_to, parse_mode="md"
                )
            except Exception:
                pass
        return None
    finally:
        cleanup(file)


# =====================================================================
#  核心修复：下载重传（相册）—— 正确传 caption 和 spoiler
# =====================================================================

async def _send_album_by_upload(
    client: TelegramClient,
    recipient: EntityLike,
    grouped_messages: List[Message],
    caption: Optional[str],
    reply_to: Optional[int],
    preserve_spoiler: bool = False,
) -> Union[Message, List[Message], None]:
    """
    下载所有媒体后重新上传为相册。
    关键：使用文件路径列表而非 Message 对象，确保 caption 和视频都生效。
    """
    files: List[str] = []
    for msg in grouped_messages:
        if _msg_has_media(msg):
            file = await _safe_download_media(msg, client)
            if file:
                files.append(file)
            else:
                logging.warning(f"⚠️ 相册成员 {msg.id} 下载失败，跳过")

    if not files:
        if caption:
            return await client.send_message(
                recipient, caption, reply_to=reply_to, parse_mode="md"
            )
        return None

    try:
        kwargs = {
            'entity': recipient,
            'file': files,
            'caption': caption or "",
            'reply_to': reply_to,
            'supports_streaming': True,
            'force_document': False,
            'allow_cache': False,
            'parse_mode': "md",
        }
        if preserve_spoiler and _SUPPORTS_SPOILER:
            kwargs['has_spoiler'] = True
        result = await client.send_file(**kwargs)
        logging.info(f"✅ 相册下载重传成功 ({len(files)} 个文件)")
        return result
    except Exception as e:
        logging.error(f"❌ 相册下载重传失败: {e}")
        # 兜底：逐条发送
        results = []
        for i, file in enumerate(files):
            try:
                send_kwargs = {
                    'entity': recipient,
                    'file': file,
                    'caption': (caption or "") if i == 0 else "",
                    'reply_to': reply_to if i == 0 else None,
                    'supports_streaming': True,
                    'parse_mode': "md",
                }
                if preserve_spoiler and _SUPPORTS_SPOILER:
                    send_kwargs['has_spoiler'] = True
                r = await client.send_file(**send_kwargs)
                results.append(r)
            except Exception as e2:
                logging.error(f"❌ 逐条发送失败 ({i}): {e2}")
        return results if results else None
    finally:
        cleanup(*files)


async def _refetch_message(client: TelegramClient, message: Message) -> Message:
    try:
        fresh = await client.get_messages(message.chat_id, ids=message.id)
        return fresh or message
    except Exception:
        return message


# =====================================================================
#  Spoiler 单条发送（带自动回退）
# =====================================================================

async def _send_single_with_spoiler(
    client: TelegramClient,
    recipient: EntityLike,
    message: Message,
    caption: Optional[str] = None,
    reply_to: Optional[int] = None,
) -> Message:
    media = message.media
    peer = await client.get_input_entity(recipient)
    input_media = _build_input_media(media, spoiler=True)
    if input_media is None:
        raise ValueError(f"不支持的媒体类型: {type(media)}")

    msg_text, msg_entities = _parse_caption_entities(caption or "")
    kwargs = {
        'peer': peer,
        'media': input_media,
        'message': msg_text,
        'random_id': random.randrange(-2 ** 63, 2 ** 63),
    }
    if msg_entities:
        kwargs['entities'] = msg_entities
    if reply_to is not None:
        kwargs['reply_to'] = _make_reply_to(reply_to)

    try:
        result = await client(SendMediaRequest(**kwargs))
        if hasattr(result, 'updates'):
            for update in result.updates:
                if hasattr(update, 'message'):
                    return update.message
        return result
    except Exception as e:
        if _is_file_reference_error(e):
            logging.warning("⚠️ spoiler file_reference 过期，尝试刷新")
            refreshed = await _refetch_message(client, message)
            try:
                input_media = _build_input_media(refreshed.media, spoiler=True)
                if input_media is not None:
                    kwargs['media'] = input_media
                    result = await client(SendMediaRequest(**kwargs))
                    if hasattr(result, 'updates'):
                        for update in result.updates:
                            if hasattr(update, 'message'):
                                return update.message
                    return result
            except Exception as e2:
                if not _is_file_reference_error(e2):
                    logging.warning(f"⚠️ spoiler 刷新后发送失败: {e2}")
            logging.warning("⚠️ spoiler 刷新失败，下载重传")
            result = await _send_single_by_upload(
                client, recipient, message, caption, reply_to,
                preserve_spoiler=True,
            )
            if result:
                return result
        raise


# =====================================================================
#  Spoiler 相册发送（带自动回退）
# =====================================================================

async def _send_album_with_spoiler(
    client: TelegramClient,
    recipient: EntityLike,
    grouped_messages: List[Message],
    caption: Optional[str] = None,
    reply_to: Optional[int] = None,
) -> List[Message]:
    peer = await client.get_input_entity(recipient)
    multi_media = []
    for i, msg in enumerate(grouped_messages):
        media = msg.media
        is_spoiler = _has_spoiler(msg)
        input_media = _build_input_media(media, spoiler=is_spoiler)
        if input_media is None:
            continue
        if i == 0 and caption:
            msg_text, msg_entities = _parse_caption_entities(caption)
        else:
            msg_text = ""
            msg_entities = []
        single = InputSingleMedia(
            media=input_media,
            random_id=random.randrange(-2 ** 63, 2 ** 63),
            message=msg_text,
            entities=msg_entities if msg_entities else [],
        )
        multi_media.append(single)

    if not multi_media:
        raise ValueError("没有有效的媒体可发送")

    kwargs = {'peer': peer, 'multi_media': multi_media}
    if reply_to is not None:
        kwargs['reply_to'] = _make_reply_to(reply_to)

    try:
        result = await client(SendMultiMediaRequest(**kwargs))
        sent = []
        if hasattr(result, 'updates'):
            for update in result.updates:
                if hasattr(update, 'message'):
                    sent.append(update.message)
        return sent if sent else []
    except Exception as e:
        if _is_file_reference_error(e):
            logging.warning("⚠️ spoiler 相册 file_reference 过期，尝试刷新")
            refreshed_messages = []
            for msg in grouped_messages:
                refreshed_messages.append(await _refetch_message(client, msg))
            try:
                multi_media = []
                for i, msg in enumerate(refreshed_messages):
                    media = msg.media
                    input_media = _build_input_media(media, spoiler=True)
                    if input_media is None:
                        continue
                    if i == 0 and caption:
                        msg_text, msg_entities = _parse_caption_entities(caption)
                    else:
                        msg_text = ""
                        msg_entities = []
                    single = InputSingleMedia(
                        media=input_media,
                        random_id=random.randrange(-2 ** 63, 2 ** 63),
                        message=msg_text,
                        entities=msg_entities if msg_entities else [],
                    )
                    multi_media.append(single)
                if multi_media:
                    kwargs = {'peer': peer, 'multi_media': multi_media}
                    if reply_to is not None:
                        kwargs['reply_to'] = _make_reply_to(reply_to)
                    result = await client(SendMultiMediaRequest(**kwargs))
                    sent = []
                    if hasattr(result, 'updates'):
                        for update in result.updates:
                            if hasattr(update, 'message'):
                                sent.append(update.message)
                    if sent:
                        return sent
            except Exception as e2:
                if not _is_file_reference_error(e2):
                    logging.warning(f"⚠️ spoiler 相册刷新后发送失败: {e2}")
            logging.warning("⚠️ spoiler 相册刷新失败，下载重传")
            result = await _send_album_by_upload(
                client, recipient, grouped_messages,
                caption, reply_to, preserve_spoiler=True,
            )
            if isinstance(result, list):
                return result
            return [result] if result else []
        raise


# =====================================================================
#  普通相册发送（InputMedia 引用，不下载）
# =====================================================================

async def _send_album_via_input_media(
    client: TelegramClient,
    recipient: EntityLike,
    grouped_messages: List[Message],
    caption: Optional[str] = None,
    reply_to: Optional[int] = None,
) -> Optional[List[Message]]:
    peer = await client.get_input_entity(recipient)
    multi_media = []
    for i, msg in enumerate(grouped_messages):
        media = msg.media
        if media is None:
            continue
        is_spoiler = _has_spoiler(msg)
        input_media = _build_input_media(media, spoiler=is_spoiler)
        if input_media is None:
            continue
        if i == 0 and caption:
            msg_text, msg_entities = _parse_caption_entities(caption)
        else:
            msg_text = ""
            msg_entities = []
        single = InputSingleMedia(
            media=input_media,
            random_id=random.randrange(-2 ** 63, 2 ** 63),
            message=msg_text,
            entities=msg_entities if msg_entities else [],
        )
        multi_media.append(single)

    if not multi_media:
        return None

    kwargs = {'peer': peer, 'multi_media': multi_media}
    if reply_to is not None:
        kwargs['reply_to'] = _make_reply_to(reply_to)

    try:
        result = await client(SendMultiMediaRequest(**kwargs))
        sent = []
        if hasattr(result, 'updates'):
            for update in result.updates:
                if hasattr(update, 'message'):
                    sent.append(update.message)
        return sent if sent else []
    except Exception as e:
        if _is_file_reference_error(e):
            logging.warning("⚠️ InputMedia 相册 file_reference 过期，尝试刷新")
            refreshed_messages = []
            for msg in grouped_messages:
                refreshed_messages.append(await _refetch_message(client, msg))
            multi_media = []
            for i, msg in enumerate(refreshed_messages):
                media = msg.media
                if media is None:
                    continue
                is_spoiler = _has_spoiler(msg)
                input_media = _build_input_media(media, spoiler=is_spoiler)
                if input_media is None:
                    continue
                if i == 0 and caption:
                    msg_text, msg_entities = _parse_caption_entities(caption)
                else:
                    msg_text = ""
                    msg_entities = []
                single = InputSingleMedia(
                    media=input_media,
                    random_id=random.randrange(-2 ** 63, 2 ** 63),
                    message=msg_text,
                    entities=msg_entities if msg_entities else [],
                )
                multi_media.append(single)
            if not multi_media:
                return None
            kwargs = {'peer': peer, 'multi_media': multi_media}
            if reply_to is not None:
                kwargs['reply_to'] = _make_reply_to(reply_to)
            try:
                result = await client(SendMultiMediaRequest(**kwargs))
                sent = []
                if hasattr(result, 'updates'):
                    for update in result.updates:
                        if hasattr(update, 'message'):
                            sent.append(update.message)
                return sent if sent else []
            except Exception as e2:
                logging.warning(f"⚠️ InputMedia 相册刷新后失败: {e2}")
        else:
            logging.warning(f"⚠️ InputMedia 相册失败: {e}")
        return None


# =====================================================================
#  核心修复：通过 SendMediaRequest 发送单条带媒体消息
#  确保 caption 正确附加，不依赖 client.send_file(MessageMedia)
# =====================================================================

async def _send_media_with_caption(
    client: TelegramClient,
    recipient: EntityLike,
    message: Message,
    caption: Optional[str],
    reply_to: Optional[int],
    buttons=None,
) -> Optional[Message]:
    """
    通过底层 SendMediaRequest 发送带媒体的消息。
    确保 caption 正确设置（解决 client.send_file(MessageMedia) 忽略 caption 的问题）。
    如果 file_reference 过期则返回 None 交给调用方回退。
    """
    media = message.media
    if media is None:
        return None

    peer = await client.get_input_entity(recipient)
    is_spoiler = _has_spoiler(message)
    input_media = _build_input_media(media, spoiler=is_spoiler)

    if input_media is None:
        # 不支持的类型，回退到 None
        return None

    msg_text, msg_entities = _parse_caption_entities(caption or "")

    kwargs = {
        'peer': peer,
        'media': input_media,
        'message': msg_text,
        'random_id': random.randrange(-2 ** 63, 2 ** 63),
    }
    if msg_entities:
        kwargs['entities'] = msg_entities
    if reply_to is not None:
        kwargs['reply_to'] = _make_reply_to(reply_to)
    # 注意：SendMediaRequest 不支持 buttons 参数
    # 带按钮需要通过 bot API 或 send_message

    try:
        result = await client(SendMediaRequest(**kwargs))
        if hasattr(result, 'updates'):
            for update in result.updates:
                if hasattr(update, 'message'):
                    return update.message
        return result
    except Exception as e:
        if _is_file_reference_error(e):
            logging.warning("⚠️ file_reference 过期，尝试刷新")
            refreshed = await _refetch_message(client, message)
            input_media = _build_input_media(refreshed.media, spoiler=is_spoiler)
            if input_media is None:
                return None
            kwargs['media'] = input_media
            try:
                result = await client(SendMediaRequest(**kwargs))
                if hasattr(result, 'updates'):
                    for update in result.updates:
                        if hasattr(update, 'message'):
                            return update.message
                return result
            except Exception as e2:
                logging.warning(f"⚠️ 刷新后发送失败: {e2}")
        raise


# =====================================================================
#  辅助
# =====================================================================

def _get_download_client(tm: "NbMessage") -> TelegramClient:
    msg_client = getattr(tm.message, '_client', None) or getattr(tm.message, 'client', None)
    if msg_client is not None:
        return msg_client
    return tm.client


# =====================================================================
#  核心发送函数（彻底修复文本丢失和视频丢失）
# =====================================================================

async def send_message(
    recipient: EntityLike,
    tm: "NbMessage",
    grouped_messages: Optional[List[Message]] = None,
    grouped_tms: Optional[List["NbMessage"]] = None,
    grouped_caption: Optional[str] = None,
    comment_to_post: Optional[int] = None,
) -> Union[Message, List[Message], None]:
    client: TelegramClient = tm.client
    effective_reply_to = comment_to_post if comment_to_post else tm.reply_to
    should_preserve_spoiler = _has_spoiler(tm.message)
    can_forward_grouped_directly = bool(
        grouped_messages and _can_forward_grouped_messages_directly(
            grouped_messages, grouped_caption=grouped_caption, grouped_tms=grouped_tms,
        )
    )
    logging.info(
        f"📤 send_message 开始 recipient={recipient} effective_reply_to={effective_reply_to} "
        f"grouped_count={len(grouped_messages) if grouped_messages else 0} "
        f"show_forwarded_from={CONFIG.show_forwarded_from} can_forward_grouped_directly={can_forward_grouped_directly} "
        f"source={describe_message(tm.message)} tm={describe_nb_message(tm)}"
    )

    # ================================================================
    # 1. 转发消息 (Show Forwarded From)
    # ================================================================
    if CONFIG.show_forwarded_from:
        if grouped_messages and can_forward_grouped_directly:
            attempt = 0
            while attempt < MAX_RETRIES:
                try:
                    result = await client.forward_messages(recipient, grouped_messages)
                    logging.info("✅ 直接转发媒体组成功")
                    return result
                except Exception as e:
                    if _is_file_reference_error(e):
                        logging.warning("⚠️ 直接转发媒体组 file_reference 过期，尝试刷新")
                        refreshed_messages = []
                        for msg in grouped_messages:
                            refreshed_messages.append(await _refetch_message(client, msg))
                        try:
                            result = await client.forward_messages(recipient, refreshed_messages)
                            logging.info("✅ 直接转发媒体组刷新后成功")
                            return result
                        except Exception as e2:
                            logging.warning(f"⚠️ 直接转发媒体组刷新后失败: {e2}")
                    if _is_protected_chat_error(e):
                        combined_caption = (
                            grouped_caption if grouped_caption is not None
                            else "\n\n".join(
                                [gtm.text.strip() for gtm in grouped_tms or [] if gtm.text and gtm.text.strip()]
                            )
                        )
                        return await _send_album_by_upload(
                            client, recipient, grouped_messages,
                            combined_caption or None, effective_reply_to,
                            preserve_spoiler=_any_has_spoiler(grouped_messages),
                        )
                    if _is_disconnected_error(e):
                        logging.error(f"❌ 转发失败(断连): {e}")
                        return None
                    if _is_flood_wait(e):
                        await _handle_flood_wait(e)
                    else:
                        logging.error(f"❌ 转发失败: {e}")
                    attempt += 1
                    await asyncio.sleep(5)
            return None
        elif not grouped_messages and not should_preserve_spoiler:
            attempt = 0
            while attempt < MAX_RETRIES:
                try:
                    result = await client.forward_messages(
                        recipient, tm.message.id, from_peer=tm.message.chat_id,
                    )
                    if isinstance(result, list):
                        result = result[0] if result else None
                    logging.info(f"✅ forward 成功 msg={tm.message.id}")
                    return result
                except Exception as e:
                    if _is_file_reference_error(e):
                        logging.warning("⚠️ 直接转发 file_reference 过期，尝试刷新")
                        refreshed = await _refetch_message(client, tm.message)
                        try:
                            result = await client.forward_messages(recipient, refreshed)
                            if isinstance(result, list):
                                result = result[0] if result else None
                            logging.info(f"✅ forward 刷新后成功 msg={tm.message.id}")
                            return result
                        except Exception as e2:
                            logging.warning(f"⚠️ 直接转发刷新后失败: {e2}")
                    if _is_protected_chat_error(e):
                        if getattr(tm.message, "media", None):
                            return await _send_single_by_upload(
                                client, recipient, tm.message,
                                tm.text, effective_reply_to,
                                preserve_spoiler=_has_spoiler(tm.message),
                            )
                        else:
                            try:
                                return await client.send_message(
                                    recipient, tm.text or "",
                                    reply_to=effective_reply_to, parse_mode="md",
                                )
                            except Exception:
                                return None
                    if _is_disconnected_error(e):
                        logging.error(f"❌ forward 失败(断连): {e}")
                        return None
                    if _is_flood_wait(e):
                        await _handle_flood_wait(e)
                    else:
                        logging.error(f"❌ forward 失败: {e}")
                    attempt += 1
                    await asyncio.sleep(5)
            return None

    # ================================================================
    # 2. 媒体组发送 (Send Album)
    # ================================================================
    if grouped_messages and grouped_tms:
        if len(grouped_messages) > 10:
            logging.warning(
                f"⚠️ 媒体组共 {len(grouped_messages)} 条，自动拆分为最多 10 条/组发送"
            )
            source_caption = (
                grouped_caption if grouped_caption is not None
                else "\n\n".join(
                    [gtm.text.strip() for gtm in grouped_tms if gtm.text and gtm.text.strip()]
                )
            )
            sent_messages = []
            grouped_pairs = list(zip(grouped_messages, grouped_tms))
            for index in range(0, len(grouped_pairs), 10):
                pair_chunk = grouped_pairs[index:index + 10]
                message_chunk = [message for message, _ in pair_chunk]
                tm_chunk = [chunk_tm for _, chunk_tm in pair_chunk]
                chunk_result = await send_message(
                    recipient,
                    tm_chunk[0],
                    grouped_messages=message_chunk,
                    grouped_tms=tm_chunk,
                    grouped_caption=source_caption or None,
                    comment_to_post=comment_to_post if index == 0 else None,
                )
                if isinstance(chunk_result, list):
                    sent_messages.extend([msg for msg in chunk_result if msg is not None])
                elif chunk_result is not None:
                    sent_messages.append(chunk_result)
            return sent_messages if sent_messages else None

        combined_caption = (
            grouped_caption if grouped_caption is not None
            else "\n\n".join(
                [gtm.text.strip() for gtm in grouped_tms if gtm.text and gtm.text.strip()]
            )
        )
        any_spoiler = _any_has_spoiler(grouped_messages)
        attempt = 0
        while attempt < MAX_RETRIES:
            try:
                if any_spoiler:
                    # spoiler 相册：引用方式，内部自动回退下载重传
                    result = await _send_album_with_spoiler(
                        client, recipient, grouped_messages,
                        caption=combined_caption or None,
                        reply_to=effective_reply_to,
                    )
                else:
                    # 先尝试 InputMedia 引用（不下载，保留视频）
                    result = await _send_album_via_input_media(
                        client, recipient, grouped_messages,
                        caption=combined_caption or None,
                        reply_to=effective_reply_to,
                    )
                    if result is None:
                        # 引用失败（file_reference 过期等），下载重传
                        logging.warning("⚠️ 引用方式失败，下载重传")
                        result = await _send_album_by_upload(
                            client, recipient, grouped_messages,
                            combined_caption or None, effective_reply_to,
                            preserve_spoiler=any_spoiler,
                        )
                logging.info("✅ 媒体组发送成功")
                return result
            except Exception as e:
                if _is_protected_chat_error(e):
                    return await _send_album_by_upload(
                        client, recipient, grouped_messages,
                        combined_caption or None, effective_reply_to,
                        preserve_spoiler=any_spoiler,
                    )
                if _is_disconnected_error(e):
                    logging.error(f"❌ 媒体组发送失败(断连): {e}")
                    return None
                if _is_flood_wait(e):
                    await _handle_flood_wait(e)
                else:
                    logging.error(f"❌ 媒体组发送失败: {e}")
                attempt += 1
                await asyncio.sleep(5)
        return None

    # ================================================================
    # 3. 单条消息发送
    # ================================================================
    processed_markup = getattr(tm, 'reply_markup', None)

    # 3a. 插件生成的新文件
    if tm.new_file:
        is_spoiler = _has_spoiler(tm.message)
        try:
            send_kwargs = {
                'entity': recipient,
                'file': tm.new_file,
                'caption': tm.text,
                'reply_to': effective_reply_to,
                'supports_streaming': True,
                'buttons': processed_markup,
            }
            if is_spoiler and _SUPPORTS_SPOILER:
                send_kwargs['has_spoiler'] = True
            return await client.send_file(**send_kwargs)
        except Exception as e:
            logging.warning(f"⚠️ 新文件发送失败: {e}")
            try:
                retry_kwargs = {
                    'entity': recipient,
                    'file': tm.new_file,
                    'caption': tm.text,
                    'reply_to': effective_reply_to,
                    'supports_streaming': True,
                }
                if is_spoiler and _SUPPORTS_SPOILER:
                    retry_kwargs['has_spoiler'] = True
                return await client.send_file(**retry_kwargs)
            except Exception as e2:
                logging.error(f"❌ 新文件发送最终失败: {e2}")
                return None

    # 3b. Spoiler 媒体
    if _has_spoiler(tm.message):
        try:
            result = await _send_single_with_spoiler(
                client, recipient, tm.message,
                caption=tm.text, reply_to=effective_reply_to,
            )
            logging.info("✅ Spoiler 消息发送成功")
            return result
        except Exception as e:
            logging.warning(f"⚠️ spoiler 发送失败，下载重传: {e}")
            return await _send_single_by_upload(
                client, recipient, tm.message,
                tm.text, effective_reply_to,
                preserve_spoiler=True,
            )

    # 3c. 带媒体的普通消息（核心修复点）
    has_media = getattr(tm.message, "media", None) is not None

    if has_media:
        attempt = 0
        while attempt < MAX_RETRIES:
            try:
                # === 方案1：用底层 SendMediaRequest 确保 caption 生效 ===
                result = await _send_media_with_caption(
                    client, recipient, tm.message,
                    caption=tm.text,
                    reply_to=effective_reply_to,
                    buttons=processed_markup,
                )
                if result is not None:
                    logging.info(f"✅ 媒体消息发送成功 (SendMediaRequest) msg={tm.message.id}")
                    return result

                # _send_media_with_caption 返回 None = 不支持的媒体类型
                # 回退到下载重传
                logging.warning("⚠️ 不支持的媒体类型，下载重传")
                return await _send_single_by_upload(
                    client, recipient, tm.message,
                    tm.text, effective_reply_to,
                    preserve_spoiler=should_preserve_spoiler,
                )

            except Exception as e:
                if _is_file_reference_error(e):
                    # file_reference 过期：下载重传
                    logging.warning(f"⚠️ file_reference 过期，下载重传: {e}")
                    return await _send_single_by_upload(
                        client, recipient, tm.message,
                        tm.text, effective_reply_to,
                        preserve_spoiler=should_preserve_spoiler,
                    )
                if _is_protected_chat_error(e):
                    logging.warning("⚠️ 受保护聊天，下载重传")
                    return await _send_single_by_upload(
                        client, recipient, tm.message,
                        tm.text, effective_reply_to,
                        preserve_spoiler=should_preserve_spoiler,
                    )
                if _is_disconnected_error(e):
                    logging.error(f"❌ 媒体消息发送失败(断连): {e}")
                    return None
                if _is_flood_wait(e):
                    await _handle_flood_wait(e)
                else:
                    logging.error(f"❌ 媒体消息发送失败: {e}")
                attempt += 1
                await asyncio.sleep(5)

        # 所有重试失败，最终回退下载重传
        return await _send_single_by_upload(
            client, recipient, tm.message,
            tm.text, effective_reply_to,
            preserve_spoiler=should_preserve_spoiler,
        )

    # 3d. 纯文本消息
    attempt = 0
    while attempt < MAX_RETRIES:
        try:
            if processed_markup is not None:
                try:
                    result = await client.send_message(
                        recipient, tm.text or "",
                        reply_to=effective_reply_to,
                        buttons=processed_markup,
                        parse_mode="md",
                    )
                    logging.info(f"✅ 文本消息(带按钮) msg={tm.message.id}")
                    return result
                except Exception as e_btn:
                    logging.warning(f"⚠️ 带按钮失败: {e_btn}")

            result = await client.send_message(
                recipient, tm.text or "",
                reply_to=effective_reply_to,
                parse_mode="md",
            )
            logging.info(f"✅ 文本消息 msg={tm.message.id}")
            return result
        except Exception as e:
            if _is_flood_wait(e):
                await _handle_flood_wait(e)
            else:
                if _is_protected_chat_error(e):
                    try:
                        return await client.send_message(
                            recipient, tm.text or "",
                            reply_to=effective_reply_to,
                        )
                    except Exception:
                        pass
                logging.error(f"❌ 文本消息失败: {e}")
            attempt += 1
            await asyncio.sleep(5)
    return None


# =====================================================================
#  清理 / 工具
# =====================================================================

def cleanup(*files: str) -> None:
    for file in files:
        if not file:
            continue
        try:
            if os.path.isfile(file):
                os.remove(file)
        except Exception as e:
            logging.debug(f"⚠️ 清理失败 {file}: {e}")


def stamp(file: str, user: str) -> str:
    now = str(datetime.now())
    outf = safe_name(f"{user} {now} {file}")
    try:
        os.rename(file, outf)
        return outf
    except Exception:
        return file


def safe_name(string: str) -> str:
    return re.sub(pattern=r"[-!@#$%^&*()\s]", repl="_", string=string)


def match(pattern: str, string: str, regex: bool) -> bool:
    if regex:
        return bool(re.findall(pattern, string))
    return pattern in string


def replace(pattern: str, new: str, string: str, regex: bool) -> str:
    def fmt_repl(matched):
        style = new
        code = STYLE_CODES.get(style)
        return f"{code}{matched.group(0)}{code}" if code else new

    if regex:
        if new in STYLE_CODES:
            return re.compile(pattern).sub(repl=fmt_repl, string=string)
        return re.sub(pattern, new, string)
    else:
        if new in STYLE_CODES:
            code = STYLE_CODES[new]
            return string.replace(pattern, f"{code}{pattern}{code}")
        return string.replace(pattern, new)


# =====================================================================
#  评论区媒体合并收集
# =====================================================================

async def collect_all_comment_media(
    client: TelegramClient,
    channel_id: Union[int, str],
    post_id: int,
    comments_cfg,
) -> List[Message]:
    """
    收集帖子评论区中所有带媒体的消息。
    用于评论区媒体合并模式：将主消息 + 评论区媒体作为一个媒体组转发。

    Args:
        client: Telegram 客户端
        channel_id: 频道 ID
        post_id: 帖子 ID
        comments_cfg: CommentsConfig 实例
    Returns:
        带媒体的评论消息列表
    """
    disc_msg = await get_discussion_message(client, channel_id, post_id)
    if disc_msg is None:
        return []

    media_messages: List[Message] = []
    try:
        async for comment in client.iter_messages(
            disc_msg.chat_id, reply_to=disc_msg.id,
            reverse=True, limit=CONFIG.bot_media.recent_limit,
        ):
            if isinstance(comment, MessageService):
                continue

            # 跳过频道转发副本（讨论区顶部的帖子镜像）
            if hasattr(comment, 'fwd_from') and comment.fwd_from:
                if getattr(comment.fwd_from, 'channel_post', None):
                    continue

            # 跳过 bot 评论
            if getattr(comments_cfg, 'skip_bot_comments', False):
                try:
                    sender = await comment.get_sender()
                    if sender and getattr(sender, 'bot', False):
                        continue
                except Exception:
                    pass

            # 跳过管理员评论
            if getattr(comments_cfg, 'skip_admin_comments', False):
                try:
                    from telethon.tl.functions.channels import GetParticipantRequest
                    from telethon.tl.types import (
                        ChannelParticipantAdmin,
                        ChannelParticipantCreator,
                    )
                    participant_result = await client(
                        GetParticipantRequest(channel_id, comment.sender_id)
                    )
                    p = participant_result.participant
                    if isinstance(p, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                        continue
                except Exception:
                    pass

            # 只收集带媒体的消息
            if _msg_has_media(comment):
                media_messages.append(comment)

    except MsgIdInvalidError as e:
        logging.warning(f"⚠️ 讨论区消息 ID 无效 post={post_id}: {e}")
    except Exception as e:
        logging.warning(f"⚠️ 评论区媒体收集失败 post={post_id}: {e}")

    logging.info(
        f"📦 评论区媒体收集完成 post={post_id}: {len(media_messages)} 个媒体文件"
    )
    return media_messages


# =====================================================================
#  共用辅助函数（供 live.py / past.py 共用）
# =====================================================================

def extract_msg_id(fwded) -> Optional[int]:
    """从转发结果中提取消息 ID"""
    if fwded is None:
        return None
    if isinstance(fwded, int):
        return fwded
    if isinstance(fwded, list):
        if fwded and hasattr(fwded[0], 'id'):
            return fwded[0].id
        return None
    if hasattr(fwded, 'id'):
        return fwded.id
    return None


def dedupe_messages(messages: List[Message]) -> List[Message]:
    """按消息 ID 去重"""
    seen = set()
    result = []
    for msg in messages:
        if msg.id in seen:
            continue
        seen.add(msg.id)
        result.append(msg)
    return result


def chunk_list(items: List, size: int) -> List[List]:
    """将列表按 size 分块"""
    return [items[i:i + size] for i in range(0, len(items), size)]


def bot_media_allowed(forward) -> bool:
    """检查 forward 配置是否允许 bot 媒体"""
    return forward is None or forward.bot_media_enabled is not False


def clean_session_files():
    for item in os.listdir():
        if item.endswith(".session") or item.endswith(".session-journal"):
            os.remove(item)
            logging.info(f"🧹 删除会话文件: {item}")
