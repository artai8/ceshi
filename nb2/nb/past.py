# nb/past.py

import asyncio
import logging
from collections import defaultdict
from typing import List, Dict, Optional, Union

from telethon import TelegramClient
from telethon.errors.rpcerrorlist import FloodWaitError, MsgIdInvalidError
from telethon.tl.custom.message import Message
from telethon.tl.patched import MessageService

from nb import config
from nb import storage as st
from nb.const import FAILED_FORWARD_LOG_FILE_NAME
from nb.config import CONFIG, get_SESSION, write_config
from nb.plugins import apply_plugins, apply_plugins_to_group, load_async_plugins
from nb.utils import (
    clean_session_files,
    send_message,
    _get_reply_to_msg_id,
    _get_reply_to_top_id,
    get_discussion_message,
    get_discussion_group_id,
    _extract_comment_keyword,
    _collect_start_links_from_keyword_reply,
    _collect_from_start_links,
    _filter_bot_media_by_blacklist,
    _trim_keyword,
    resolve_bot_media_from_message,
    trigger_comment_keyword_and_resolve_bot_media,
    _msg_has_media,
    extract_msg_id as _extract_msg_id,
    dedupe_messages as _dedupe_messages,
    chunk_list as _chunk_list,
    bot_media_allowed as _bot_media_allowed,
    collect_all_comment_media,
    get_random_forward_delay,
    describe_message,
    describe_nb_message,
)


# =====================================================================
#  断连检测 & 自动重连
# =====================================================================

_MAX_RECONNECT_ATTEMPTS = 2

async def _ensure_connected(client: TelegramClient) -> None:
    """检测客户端是否断连，如已断连则尝试重连。"""
    if client.is_connected():
        return
    for attempt in range(1, _MAX_RECONNECT_ATTEMPTS + 1):
        logging.warning(f"⚠️ 检测到断连, 尝试重连 ({attempt}/{_MAX_RECONNECT_ATTEMPTS})")
        try:
            await client.connect()
            if client.is_connected():
                logging.info("✅ 重连成功")
                return
        except Exception as e:
            logging.error(f"❌ 重连失败: {e}")
        await asyncio.sleep(5 * attempt)
    logging.critical("🚨 多次重连均失败, 将继续尝试后续操作")


def _resolve_reply_to_id(
    message: Message,
    dest: int,
) -> Optional[int]:
    """从 stored 映射中查找目标频道中对应的 reply_to msg id。"""
    if not getattr(message, 'is_reply', False):
        return None
    reply_msg_id = _get_reply_to_msg_id(message)
    if reply_msg_id is None:
        return None
    r_event = st.DummyEvent(message.chat_id, reply_msg_id)
    r_event_uid = st.EventUid(r_event)
    if r_event_uid not in st.stored:
        return None
    fwded_reply = st.stored[r_event_uid].get(dest)
    if fwded_reply is None:
        return None
    return _extract_msg_id(fwded_reply)


# =====================================================================
#  Bot 媒体从评论区收集
# =====================================================================

async def _collect_bot_media_from_comments(
    client: TelegramClient,
    src_channel_id: int,
    src_post_id: int,
    forward,
) -> List[Message]:
    if not _bot_media_allowed(forward):
        logging.info(f"🤖 bot_media 未启用, 跳过 post={src_post_id}")
        return []

    logging.info(f"🤖 开始获取讨论消息 channel={src_channel_id} post={src_post_id}")

    try:
        disc_msg = await get_discussion_message(client, src_channel_id, src_post_id)
    except Exception as e:
        logging.warning(f"⚠️ 获取讨论消息异常 post={src_post_id}: {e}")
        return []

    if disc_msg is None:
        logging.info(f"🤖 帖子 {src_post_id} 无讨论消息, 跳过")
        return []

    src_discussion_id = disc_msg.chat_id
    src_top_id = disc_msg.id
    logging.info(f"🤖 讨论组={src_discussion_id} top_id={src_top_id}")

    comment_count = 0
    collected: List[Message] = []
    counts = {}
    keyword_hint = None

    try:
        async for comment in client.iter_messages(
            src_discussion_id, reply_to=src_top_id, reverse=True,
            limit=CONFIG.bot_media.recent_limit,
        ):
            if isinstance(comment, MessageService):
                logging.debug(f"🤖 跳过 MessageService #{comment.id}")
                continue

            comment_count += 1
            text_preview = (comment.raw_text or comment.text or "")[:150]
            has_markup = comment.reply_markup is not None
            sender_id = comment.sender_id
            fwd = comment.fwd_from
            logging.debug(
                f"🤖 评论#{comment.id} sender={sender_id} fwd={fwd is not None} "
                f"markup={has_markup} text={text_preview!r}"
            )
            text = _trim_keyword((comment.raw_text or comment.text or "").strip())
            if text:
                counts[text] = counts.get(text, 0) + 1
                if counts[text] >= 5:
                    keyword_hint = text
                    break

            try:
                bot_media = await resolve_bot_media_from_message(client, comment, forward)
            except Exception as e:
                logging.warning(f"⚠️ 评论#{comment.id} bot媒体解析异常: {e}")
                bot_media = []

            if bot_media:
                logging.info(f"🤖 ✅ 评论#{comment.id} 命中 {len(bot_media)} 条bot媒体")
                collected.extend(bot_media)
            else:
                logging.debug(f"🤖 评论#{comment.id} 无bot媒体")
    except MsgIdInvalidError as e:
        logging.warning(f"⚠️ 讨论区消息 ID 无效, 跳过评论拉取 post={src_post_id}: {e}")
        return []

    if keyword_hint and disc_msg is not None:
        logging.info(
            f"🤖 评论区关键词命中 post={src_post_id} keyword={keyword_hint!r}"
        )
        keyword_links = await _collect_start_links_from_keyword_reply(
            client, disc_msg, keyword_hint, forward
        )
        collected_kw = await _collect_from_start_links(client, keyword_links, forward)
        if collected_kw:
            return _filter_bot_media_by_blacklist(collected_kw)
        if collected:
            return _dedupe_messages(collected)
        return []

    logging.info(
        f"🤖 评论区扫描完成 post={src_post_id}: "
        f"{comment_count} 条评论, 收集 {len(collected)} 条媒体"
    )
    return _dedupe_messages(collected) if collected else []


# =====================================================================
#  合并媒体组发送（修复 Bug1：只第一个 chunk 带 caption；修复 Bug2：支持 reply_to）
# =====================================================================

async def _send_combined_album(
    src: int,
    dest: List[int],
    first_msg_id: int,
    combined_messages: List[Message],
    reply_to_map: Optional[Dict[int, Optional[int]]] = None,
) -> bool:
    """
    发送合并后的媒体组到多个目标。
    reply_to_map: {dest_id: reply_to_msg_id} 可选的每目标回复ID映射。
    """
    tms = await apply_plugins_to_group(combined_messages)
    if not tms:
        logging.warning("⚠️ 合并媒体组全部被插件过滤，跳过")
        return False
    tm_template = tms[0]
    if tm_template is None:
        logging.warning("⚠️ 合并媒体组模板消息为 None，跳过")
        return False

    chunks = _chunk_list(tms, 10)
    # 所有 chunk 都携带相同的消息文本
    combined_caption = "\n\n".join(
        [tm.text.strip() for tm in tms if tm.text and tm.text.strip()]
    )

    for d in dest:
        try:
            fwded_first = None
            reply_to_id = (reply_to_map or {}).get(d)

            for idx, chunk in enumerate(chunks):
                if not chunk:
                    continue
                chunk_reply = reply_to_id if idx == 0 else None

                if chunk_reply is not None:
                    chunk[0].reply_to = chunk_reply

                fwded = await send_message(
                    d,
                    chunk[0],
                    grouped_messages=[tm.message for tm in chunk],
                    grouped_tms=chunk,
                    grouped_caption=combined_caption or None,
                )
                if fwded_first is None:
                    fwded_first = fwded

            event_uid = st.EventUid(st.DummyEvent(src, first_msg_id))
            st.stored[event_uid] = {d: fwded_first}
            fwded_id = _extract_msg_id(fwded_first)
            if fwded_id is not None:
                st.add_post_mapping(src, first_msg_id, d, fwded_id)
        except Exception as e:
            logging.critical(f"🚨 合并媒体组播失败: {e}")

    for tm in tms:
        tm.clear()
    return True


# =====================================================================
#  Bot 媒体相册发送
# =====================================================================

async def _send_bot_media_album(
    dest: int,
    bot_messages: List[Message],
    base_text: Optional[str] = None,
    reply_to: Optional[int] = None,
    comment_to_post: Optional[int] = None,
):
    skip_plugins = ["filter"] if CONFIG.bot_media.ignore_filter else None
    fwded_first = None
    chunks = _chunk_list(bot_messages, 10)
    for idx, chunk_msgs in enumerate(chunks):
        if not chunk_msgs:
            continue
        tms = await apply_plugins_to_group(
            chunk_msgs,
            skip_plugins=skip_plugins,
            fail_open=CONFIG.bot_media.force_forward_on_empty,
            base_text=base_text,
        )
        if not tms:
            continue
        if reply_to is not None and idx == 0:
            tms[0].reply_to = reply_to
        fwded = await send_message(
            dest,
            tms[0],
            grouped_messages=[tm.message for tm in tms],
            grouped_tms=tms,
            grouped_caption=base_text or None,
            comment_to_post=comment_to_post if idx == 0 else None,
        )
        if fwded_first is None:
            fwded_first = fwded
        for tm in tms:
            tm.clear()
    return fwded_first


# =====================================================================
#  Past 模式媒体组发送
# =====================================================================

async def _send_past_grouped(
    client: TelegramClient, src: int, dest: List[int], messages: List[Message], forward
) -> bool:
    # 评论区媒体合并模式：收集评论区所有媒体，与主消息合并后发送
    if forward and forward.comments.merge_comment_media:
        comment_media = await collect_all_comment_media(
            client, src, messages[0].id, forward.comments
        )
        if comment_media:
            combined_messages = _dedupe_messages(messages + comment_media)
        else:
            combined_messages = list(messages)
        reply_to_map = {}
        for d in dest:
            reply_to_map[d] = _resolve_reply_to_id(messages[0], d)
        return await _send_combined_album(
            src, dest, messages[0].id, combined_messages, reply_to_map=reply_to_map
        )

    comment_bot_media = await _collect_bot_media_from_comments(
        client, src, messages[0].id, forward
    )
    if comment_bot_media:
        combined_messages = messages + comment_bot_media
        # 构建 reply_to 映射
        reply_to_map = {}
        for d in dest:
            reply_to_map[d] = _resolve_reply_to_id(messages[0], d)
        return await _send_combined_album(
            src, dest, messages[0].id, combined_messages, reply_to_map=reply_to_map
        )

    bot_media = []
    bot_media_allowed = _bot_media_allowed(forward)
    auto_comment_allowed = getattr(forward, 'auto_comment_trigger_enabled', None) is not False

    if bot_media_allowed and auto_comment_allowed:
        for msg in messages:
            keyword = _extract_comment_keyword(
                msg.raw_text or msg.text or "", forward
            )
            if keyword:
                bot_media = await trigger_comment_keyword_and_resolve_bot_media(
                    client, src, msg.id, keyword, forward
                )
                break

    if bot_media:
        combined_messages = _dedupe_messages(messages + bot_media)
        reply_to_map = {}
        for d in dest:
            reply_to_map[d] = _resolve_reply_to_id(messages[0], d)
        return await _send_combined_album(
            src, dest, messages[0].id, combined_messages, reply_to_map=reply_to_map
        )

    if bot_media_allowed:
        if not bot_media:
            for msg in messages:
                # 修复 Bug7：使用传入的 client 而非 msg.client
                bot_media = await resolve_bot_media_from_message(client, msg, forward)
                if bot_media:
                    break

    if bot_media:
        combined_messages = _dedupe_messages(messages + bot_media)
        reply_to_map = {}
        for d in dest:
            reply_to_map[d] = _resolve_reply_to_id(messages[0], d)
        return await _send_combined_album(
            src, dest, messages[0].id, combined_messages, reply_to_map=reply_to_map
        )

    tms = await apply_plugins_to_group(messages)
    if not tms:
        logging.warning("⚠️ 所有消息被插件过滤，跳过该媒体组")
        return False

    tm_template = tms[0]
    if tm_template is None:
        logging.warning("⚠️ 模板消息为 None，跳过该媒体组")
        return False

    any_success = False
    for d in dest:
        try:
            reply_to_id = _resolve_reply_to_id(messages[0], d)
            tm_template.reply_to = reply_to_id

            fwded_msgs = await send_message(
                d,
                tm_template,
                grouped_messages=[tm.message for tm in tms],
                grouped_tms=tms,
            )

            if fwded_msgs is None:
                logging.error(f"❌ 媒体组发送返回 None, dest={d}")
                continue

            any_success = True
            first_msg_id = messages[0].id
            for i, original_msg in enumerate(messages):
                event_uid = st.EventUid(st.DummyEvent(src, original_msg.id))
                if event_uid not in st.stored:
                    st.stored[event_uid] = {}
                if isinstance(fwded_msgs, list) and i < len(fwded_msgs):
                    st.stored[event_uid][d] = fwded_msgs[i]
                elif not isinstance(fwded_msgs, list):
                    st.stored[event_uid][d] = fwded_msgs

            fwded_id = _extract_msg_id(fwded_msgs)
            if fwded_id is not None:
                st.add_post_mapping(src, first_msg_id, d, fwded_id)

        except Exception as e:
            logging.critical(f"🚨 组播失败: {e}")

    for tm in tms:
        tm.clear()
    return any_success


# =====================================================================
#  刷新媒体组缓冲区
# =====================================================================

async def _flush_grouped_buffer(
    client: TelegramClient,
    src: int,
    dest: List[int],
    grouped_buffer: Dict[int, List[Message]],
    forward,
) -> tuple:
    last_id = 0
    sent_groups = 0
    for gid, msgs in list(grouped_buffer.items()):
        if not msgs:
            continue

        await _ensure_connected(client)
        success = await _send_past_grouped(client, src, dest, msgs, forward)

        group_last_id = max(m.id for m in msgs)
        last_id = max(last_id, group_last_id)
        forward.offset = group_last_id
        write_config(CONFIG, persist=False)

        if success:
            logging.info(
                f"✅ 媒体组 {gid} ({len(msgs)} 条) 发送完成, offset → {group_last_id}"
            )
        else:
            st.append_failed_forward_record(
                mode="past-grouped",
                source_chat_id=src,
                source_message_id=group_last_id,
                dest_chat_ids=list(dest),
                grouped_message_ids=[m.id for m in msgs],
                reason="group send failed but offset advanced",
                details={"grouped_id": gid, "message_count": len(msgs)},
            )
            logging.error(
                f"❌ 媒体组 {gid} ({len(msgs)} 条) 发送失败, 仍推进 offset → {group_last_id}"
            )

        delay_seconds = get_random_forward_delay()
        logging.info(f"⏸️ 媒体组发送后休息 {delay_seconds} 秒")
        await asyncio.sleep(delay_seconds)

        sent_groups += 1

    grouped_buffer.clear()
    return (last_id, sent_groups)


async def _flush_grouped_chunks_if_needed(
    client: TelegramClient,
    src: int,
    dest: List[int],
    grouped_buffer: Dict[int, List[Message]],
    grouped_id: int,
    forward,
) -> tuple:
    """post 模式分段发送：当同一 grouped_id 满 10 条时先发送完整 chunk。"""
    if forward.comments.merge_comment_media or _bot_media_allowed(forward):
        return (0, 0)

    msgs = grouped_buffer.get(grouped_id, [])
    chunk_count = len(msgs) // st.GROUPED_CHUNK_SIZE
    if chunk_count <= 0:
        return (0, 0)

    send_count = chunk_count * st.GROUPED_CHUNK_SIZE
    send_msgs = msgs[:send_count]
    remaining_msgs = msgs[send_count:]

    await _ensure_connected(client)
    success = await _send_past_grouped(client, src, dest, send_msgs, forward)

    group_last_id = max(m.id for m in send_msgs)
    forward.offset = group_last_id
    write_config(CONFIG, persist=False)

    if success:
        logging.info(
            f"✅ post 分段发送 grouped_id={grouped_id} flushed={send_count} 条, offset → {group_last_id}"
        )
    else:
        st.append_failed_forward_record(
            mode="past-grouped-partial",
            source_chat_id=src,
            source_message_id=group_last_id,
            dest_chat_ids=list(dest),
            grouped_message_ids=[m.id for m in send_msgs],
            reason="partial grouped chunk send failed but offset advanced",
            details={"grouped_id": grouped_id, "message_count": len(send_msgs)},
        )
        logging.error(
            f"❌ post 分段发送失败 grouped_id={grouped_id} flushed={send_count} 条, 仍推进 offset → {group_last_id}"
        )

    delay_seconds = get_random_forward_delay()
    logging.info(f"⏸️ post 分段发送后休息 {delay_seconds} 秒")
    await asyncio.sleep(delay_seconds)

    if remaining_msgs:
        grouped_buffer[grouped_id] = remaining_msgs
    else:
        grouped_buffer.pop(grouped_id, None)

    return (group_last_id, chunk_count)


# =====================================================================
#  评论区 past 模式
# =====================================================================

async def _forward_comments_for_post(
    client: TelegramClient,
    src_channel_id: int,
    src_post_id: int,
    forward: config.Forward,
) -> None:
    comments_cfg = forward.comments

    src_disc_msg = await get_discussion_message(client, src_channel_id, src_post_id)
    if src_disc_msg is None:
        logging.debug(f"帖子 {src_post_id} 没有讨论消息，跳过评论")
        return

    src_discussion_id = src_disc_msg.chat_id
    src_top_id = src_disc_msg.id

    st.discussion_to_channel_post[(src_discussion_id, src_top_id)] = src_post_id

    dest_targets = {}

    for dest_channel_id in forward.dest:
        dest_resolved = dest_channel_id
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, dest_channel_id)
            except Exception:
                continue

        dest_post_id = st.get_dest_post_id(
            src_channel_id, src_post_id, dest_resolved
        )
        if dest_post_id is None:
            logging.debug(
                f"帖子 {src_post_id} 在目标 {dest_resolved} 没有映射，跳过评论"
            )
            continue

        if comments_cfg.dest_mode == "comments":
            dest_disc_msg = await get_discussion_message(
                client, dest_resolved, dest_post_id
            )
            if dest_disc_msg:
                dest_targets[dest_disc_msg.chat_id] = dest_disc_msg.id
                logging.info(
                    f"💬 评论目标: discussion={dest_disc_msg.chat_id}, "
                    f"reply_to={dest_disc_msg.id}"
                )
        elif comments_cfg.dest_mode == "discussion":
            for dg in comments_cfg.dest_discussion_groups:
                dg_id = dg
                if not isinstance(dg_id, int):
                    try:
                        dg_id = await config.get_id(client, dg)
                    except Exception:
                        continue
                dest_targets[dg_id] = None

    if not dest_targets:
        logging.debug(f"帖子 {src_post_id} 没有有效的评论目标")
        return

    comment_count = 0
    grouped_buffer: Dict[int, List[Message]] = defaultdict(list)

    async for comment in client.iter_messages(
        src_discussion_id, reply_to=src_top_id, reverse=True,
    ):
        if isinstance(comment, MessageService):
            continue

        if hasattr(comment, 'fwd_from') and comment.fwd_from:
            if getattr(comment.fwd_from, 'channel_post', None):
                continue

        if comments_cfg.only_media and not comment.media:
            continue
        if not comments_cfg.include_text_comments and not comment.media:
            continue
        if comments_cfg.skip_bot_comments:
            try:
                sender = await comment.get_sender()
                if sender and getattr(sender, 'bot', False):
                    continue
            except Exception:
                pass

        if comment.grouped_id is not None:
            # 刷新不同 grouped_id 的缓冲
            other_groups = [
                gid for gid in grouped_buffer if gid != comment.grouped_id
            ]
            for old_gid in other_groups:
                await _send_comment_group(
                    client, grouped_buffer[old_gid], dest_targets
                )
                comment_count += len(grouped_buffer[old_gid])
                del grouped_buffer[old_gid]
                delay = get_random_forward_delay()
                await asyncio.sleep(delay)

            grouped_buffer[comment.grouped_id].append(comment)
            continue

        # 刷新所有 grouped 缓冲
        for old_gid in list(grouped_buffer.keys()):
            await _send_comment_group(
                client, grouped_buffer[old_gid], dest_targets
            )
            comment_count += len(grouped_buffer[old_gid])
            del grouped_buffer[old_gid]
            delay = get_random_forward_delay()
            await asyncio.sleep(delay)

        # 单条评论处理
        tm = await apply_plugins(comment)
        if not tm:
            comment_count += 1
            delay = get_random_forward_delay()
            await asyncio.sleep(delay)
            continue

        bot_media = []
        bot_media_allowed = _bot_media_allowed(forward)
        if bot_media_allowed:
            bot_media = await resolve_bot_media_from_message(client, comment, forward)
        if bot_media:
            bot_media = _dedupe_messages(bot_media)
            for dest_disc_id, dest_top_id in dest_targets.items():
                try:
                    fwded = await _send_bot_media_album(
                        dest_disc_id,
                        bot_media,
                        base_text=comment.raw_text or comment.text or "",
                        comment_to_post=dest_top_id,
                    )
                    if fwded:
                        st.add_comment_mapping(
                            comment.chat_id, comment.id,
                            dest_disc_id, _extract_msg_id(fwded),
                        )
                except Exception as e:
                    logging.error(f"❌ 评论 bot 媒体发送失败: {e}")
            tm.clear()
        else:
            await _send_single_comment(client, comment, dest_targets, tm=tm)
        comment_count += 1

        delay = get_random_forward_delay()
        await asyncio.sleep(delay)

    # 刷新剩余 grouped 缓冲
    for old_gid in list(grouped_buffer.keys()):
        await _send_comment_group(
            client, grouped_buffer[old_gid], dest_targets
        )
        comment_count += len(grouped_buffer[old_gid])

    if comment_count > 0:
        logging.info(
            f"💬 帖子 {src_post_id} 评论转发完成: {comment_count} 条"
        )


async def _send_single_comment(
    client: TelegramClient,
    comment: Message,
    dest_targets: Dict[int, Optional[int]],
    tm: Optional["NbMessage"] = None,
) -> None:
    if tm is None:
        tm = await apply_plugins(comment)
        if not tm:
            return

    for dest_disc_id, dest_top_id in dest_targets.items():
        try:
            fwded = await send_message(
                dest_disc_id, tm, comment_to_post=dest_top_id
            )
            if fwded:
                st.add_comment_mapping(
                    comment.chat_id, comment.id,
                    dest_disc_id, _extract_msg_id(fwded),
                )
                logging.info(
                    f"💬 评论转发成功: {comment.chat_id}/{comment.id} → {dest_disc_id}"
                )
            else:
                logging.warning(f"⚠️ 评论转发返回 None: {comment.id}")
        except FloodWaitError as fwe:
            logging.warning(f"⛔ FloodWait (评论): {fwe.seconds} 秒")
            await asyncio.sleep(fwe.seconds + 10)
            try:
                fwded = await send_message(
                    dest_disc_id, tm, comment_to_post=dest_top_id
                )
                if fwded:
                    logging.info("💬 评论重试成功")
            except Exception as e2:
                logging.error(f"❌ 评论重试失败: {e2}")
        except Exception as e:
            logging.error(f"❌ 评论发送失败: {e}")

    tm.clear()


async def _send_comment_group(
    client: TelegramClient,
    comments: List[Message],
    dest_targets: Dict[int, Optional[int]],
) -> None:
    if not comments:
        return

    tms = await apply_plugins_to_group(comments)
    if not tms:
        return

    tm_template = tms[0]

    for dest_disc_id, dest_top_id in dest_targets.items():
        try:
            fwded = await send_message(
                dest_disc_id, tm_template,
                grouped_messages=[tm.message for tm in tms],
                grouped_tms=tms,
                comment_to_post=dest_top_id,
            )
            if fwded:
                st.add_comment_mapping(
                    comments[0].chat_id, comments[0].id,
                    dest_disc_id, _extract_msg_id(fwded),
                )
                logging.info(
                    f"💬 评论媒体组成功: {len(comments)} 条 → {dest_disc_id}"
                )
            else:
                logging.warning("⚠️ 评论媒体组返回 None")
        except FloodWaitError as fwe:
            logging.warning(f"⛔ FloodWait (评论组): {fwe.seconds} 秒")
            await asyncio.sleep(fwe.seconds + 10)
            try:
                fwded = await send_message(
                    dest_disc_id, tm_template,
                    grouped_messages=[tm.message for tm in tms],
                    grouped_tms=tms, comment_to_post=dest_top_id,
                )
                if fwded:
                    logging.info("💬 评论媒体组重试成功")
            except Exception as e2:
                logging.error(f"❌ 评论媒体组重试失败: {e2}")
        except Exception as e:
            logging.error(f"❌ 评论媒体组失败: {e}")

    for tm in tms:
        tm.clear()


# =====================================================================
#  offset 更新辅助
# =====================================================================

def _update_offset(
    forward,
    offset_key: str,
    msg_id: int,
    sources: list,
) -> None:
    """统一更新 forward offset 并持久化。"""
    forward.offsets[offset_key] = msg_id
    if len(sources) == 1:
        forward.offset = msg_id
    write_config(CONFIG, persist=False)


# =====================================================================
#  单连接带限额转发（供 past 模式和 schedule 模式共用）
# =====================================================================

async def forward_with_limit(
    client: TelegramClient,
    forward,
    max_count: int = 0,
) -> tuple:
    """
    对单个 Forward 配置执行转发，支持消息数量限额。

    Args:
        client: TelegramClient 实例
        forward: Forward 配置对象
        max_count: 最多转发消息数（0=不限制）。媒体组算作 1 条。

    Returns:
        (forwarded_count, exhausted):
            forwarded_count - 实际转发的消息数
            exhausted - True 表示来源已耗尽，False 表示因达到限额而停止
    """
    sources = config.get_forward_sources(forward)
    if not sources:
        return (0, True)

    dest = []
    for d in forward.dest:
        dest_resolved = d
        if not isinstance(dest_resolved, int):
            try:
                dest_resolved = await config.get_id(client, d)
            except Exception:
                continue
        dest.append(dest_resolved)
    if not dest:
        return (0, True)

    total_forwarded = 0
    all_exhausted = True

    for source in sources:
        if max_count > 0 and total_forwarded >= max_count:
            all_exhausted = False
            break

        src = source
        if not isinstance(src, int):
            try:
                src = await config.get_id(client, source)
            except Exception:
                continue
        offset_key = str(src)
        last_id = 0
        grouped_buffer: Dict[int, List[Message]] = defaultdict(list)
        start_offset = forward.offsets.get(offset_key, forward.offset)
        limit_reached = False
        logging.info(
            f"🚀 开始扫描来源 src={src} offset_key={offset_key} start_offset={start_offset} "
            f"dest={dest} max_count={max_count} merge_comment_media={forward.comments.merge_comment_media}"
        )

        async for message in client.iter_messages(
            src, reverse=True, offset_id=start_offset
        ):
            if isinstance(message, MessageService):
                continue

            logging.info(f"📨 取到源消息 {describe_message(message)}")

            if forward.end and message.id > forward.end:
                logging.info(f"📍 到达 end={forward.end}, 停止")
                break

            try:
                current_grouped_id = message.grouped_id

                # 刷新之前的 grouped 缓冲
                if grouped_buffer and (
                    current_grouped_id is None
                    or (
                        current_grouped_id is not None
                        and current_grouped_id not in grouped_buffer
                    )
                ):
                    group_count = len(grouped_buffer)
                    try:
                        flushed_last, flushed_groups = await _flush_grouped_buffer(
                            client, src, dest, grouped_buffer, forward
                        )
                        if flushed_last:
                            last_id = max(last_id, flushed_last)
                    except FloodWaitError as fwe:
                        logging.warning(
                            f"⛔ FloodWait (组刷新): {fwe.seconds} 秒"
                        )
                        await asyncio.sleep(fwe.seconds)
                        flushed_last, flushed_groups = await _flush_grouped_buffer(
                            client, src, dest, grouped_buffer, forward
                        )
                        if flushed_last:
                            last_id = max(last_id, flushed_last)
                    total_forwarded += flushed_groups
                    if max_count > 0 and total_forwarded >= max_count:
                        logging.info(
                            f"📊 达到转发限额 {max_count}, 停止当前连接"
                        )
                        limit_reached = True
                        break

                # grouped 消息加入缓冲
                if current_grouped_id is not None:
                    grouped_buffer[current_grouped_id].append(message)
                    logging.info(
                        f"📦 消息加入媒体组缓冲 grouped_id={current_grouped_id} "
                        f"buffer_size={len(grouped_buffer[current_grouped_id])} msg={message.id}"
                    )

                    # 与 live 模式一致：普通媒体组每满 10 条先发送一组。
                    flushed_last, flushed_chunks = await _flush_grouped_chunks_if_needed(
                        client, src, dest, grouped_buffer, current_grouped_id, forward
                    )
                    if flushed_last:
                        last_id = max(last_id, flushed_last)
                    if flushed_chunks:
                        total_forwarded += flushed_chunks
                        if max_count > 0 and total_forwarded >= max_count:
                            logging.info(
                                f"📊 达到转发限额 {max_count}, 停止当前连接"
                            )
                            limit_reached = True
                            break
                    continue

                # ============ 单条消息处理 ============
                message_sent = False
                failed_destinations = []

                bot_media_allowed = _bot_media_allowed(forward)
                auto_comment_allowed = getattr(
                    forward, 'auto_comment_trigger_enabled', None
                ) is not False
                bot_media = []

                # 自动评论关键字触发
                if bot_media_allowed and auto_comment_allowed:
                    keyword = _extract_comment_keyword(
                        message.raw_text or message.text or "", forward
                    )
                    logging.info(
                        f"🤖 自动评论关键字检测 msg={message.id} bot_media_allowed={bot_media_allowed} "
                        f"auto_comment_allowed={auto_comment_allowed} keyword={keyword!r}"
                    )
                    if keyword:
                        bot_media = await trigger_comment_keyword_and_resolve_bot_media(
                            client, src, message.id, keyword, forward
                        )
                        logging.info(
                            f"🤖 自动评论关键字结果 msg={message.id} bot_media_count={len(bot_media)}"
                        )

                # 评论区媒体合并模式
                if forward.comments.merge_comment_media:
                    logging.info(f"🧭 进入评论媒体合并分支 msg={message.id}")
                    comment_media = await collect_all_comment_media(
                        client, src, message.id, forward.comments
                    )
                    logging.info(
                        f"🧭 评论媒体合并结果 msg={message.id} comment_media_count={len(comment_media)}"
                    )
                    if comment_media:
                        combined_messages = _dedupe_messages(
                            [message] + comment_media
                        )
                    else:
                        combined_messages = [message]

                    # 如果合并后有媒体（主消息+评论媒体），走合并发送
                    if len(combined_messages) > 1 or _msg_has_media(message):
                        reply_to_map = {}
                        for d in dest:
                            reply_to_map[d] = _resolve_reply_to_id(
                                message, d
                            )
                        await _send_combined_album(
                            src, dest, message.id,
                            combined_messages,
                            reply_to_map=reply_to_map,
                        )
                        message_sent = True
                    else:
                        # 主消息无媒体且评论区也没有媒体，普通发送
                        tm = await apply_plugins(message)
                        if tm:
                            logging.info(f"🧩 单条普通发送模板 msg={message.id} tm={describe_nb_message(tm)}")
                            event_uid = st.EventUid(
                                st.DummyEvent(message.chat_id, message.id)
                            )
                            st.stored[event_uid] = {}
                            for d in dest:
                                reply_to_id = _resolve_reply_to_id(
                                    message, d
                                )
                                tm.reply_to = reply_to_id
                                try:
                                    fwded_msg = await send_message(d, tm)
                                    if fwded_msg is not None:
                                        st.stored[event_uid][d] = fwded_msg
                                        fwded_id = _extract_msg_id(fwded_msg)
                                        if fwded_id is not None:
                                            st.add_post_mapping(
                                                src, message.id, d, fwded_id
                                            )
                                except Exception as e:
                                    logging.error(f"❌ 单条发送失败: {e}")
                            tm.clear()
                            message_sent = True

                # 原有逻辑：bot 媒体收集和普通转发
                else:
                    logging.info(f"🧭 进入 bot_media/普通转发分支 msg={message.id}")
                    comment_bot_media = await _collect_bot_media_from_comments(
                        client, src, message.id, forward
                    )
                    logging.info(
                        f"🤖 评论区 bot_media 收集结果 msg={message.id} count={len(comment_bot_media)}"
                    )

                    if comment_bot_media:
                        combined_messages = [message] + comment_bot_media
                        reply_to_map = {}
                        for d in dest:
                            reply_to_map[d] = _resolve_reply_to_id(message, d)
                        await _send_combined_album(
                            src, dest, message.id, combined_messages,
                            reply_to_map=reply_to_map,
                        )
                        message_sent = True
                    else:
                        if bot_media:
                            bot_media = _dedupe_messages(bot_media)
                            has_media = _msg_has_media(message)
                            event_uid = st.EventUid(
                                st.DummyEvent(message.chat_id, message.id)
                            )
                            st.stored[event_uid] = {}

                            if has_media:
                                combined_messages = _dedupe_messages(
                                    [message] + bot_media
                                )
                                tms = await apply_plugins_to_group(
                                    combined_messages
                                )
                                logging.info(
                                    f"🧩 bot 媒体组合并模板 msg={message.id} tm_count={len(tms)}"
                                )
                                if tms:
                                    for d in dest:
                                        reply_to_id = _resolve_reply_to_id(
                                            message, d
                                        )
                                        for tm in tms:
                                            tm.reply_to = reply_to_id
                                        fwded_msg = await send_message(
                                            d,
                                            tms[0],
                                            grouped_messages=[tm.message for tm in tms],
                                            grouped_tms=tms,
                                        )
                                        if fwded_msg is not None:
                                            st.stored[event_uid][d] = fwded_msg
                                            fwded_id = _extract_msg_id(fwded_msg)
                                            if fwded_id is not None:
                                                st.add_post_mapping(
                                                    src, message.id, d, fwded_id
                                                )
                                    for tm in tms:
                                        tm.clear()
                                    message_sent = True
                            else:
                                for d in dest:
                                    reply_to_id = _resolve_reply_to_id(message, d)
                                    try:
                                        fwded_msg = await _send_bot_media_album(
                                            d, bot_media,
                                            base_text=message.raw_text or message.text or "",
                                            reply_to=reply_to_id,
                                        )
                                        if fwded_msg is not None:
                                            st.stored[event_uid][d] = fwded_msg
                                            fwded_id = _extract_msg_id(fwded_msg)
                                            if fwded_id is not None:
                                                st.add_post_mapping(
                                                    src, message.id, d, fwded_id
                                                )
                                            message_sent = True
                                    except Exception as e:
                                        logging.error(f"❌ bot 媒体发送失败: {e}")

                        if not message_sent:
                            # 从消息本身拉取 bot 媒体
                            if bot_media_allowed:
                                if not bot_media:
                                    bot_media = await resolve_bot_media_from_message(
                                        client, message, forward
                                    )
                                    logging.info(
                                        f"🤖 主消息 bot_media 解析结果 msg={message.id} count={len(bot_media)}"
                                    )

                            if bot_media:
                                bot_media = _dedupe_messages(bot_media)
                                has_media = _msg_has_media(message)
                                event_uid = st.EventUid(
                                    st.DummyEvent(message.chat_id, message.id)
                                )
                                st.stored[event_uid] = {}

                                if has_media:
                                    combined_messages = _dedupe_messages(
                                        [message] + bot_media
                                    )
                                    tms = await apply_plugins_to_group(
                                        combined_messages
                                    )
                                    logging.info(
                                        f"🧩 bot 媒体组合并模板 msg={message.id} tm_count={len(tms)}"
                                    )
                                    if tms:
                                        for d in dest:
                                            reply_to_id = _resolve_reply_to_id(
                                                message, d
                                            )
                                            for tm in tms:
                                                tm.reply_to = reply_to_id
                                            fwded_msg = await send_message(
                                                d,
                                                tms[0],
                                                grouped_messages=[tm.message for tm in tms],
                                                grouped_tms=tms,
                                            )
                                            if fwded_msg is not None:
                                                st.stored[event_uid][d] = fwded_msg
                                                fwded_id = _extract_msg_id(fwded_msg)
                                                if fwded_id is not None:
                                                    st.add_post_mapping(
                                                        src, message.id, d, fwded_id
                                                    )
                                        for tm in tms:
                                            tm.clear()
                                        message_sent = True
                                else:
                                    for d in dest:
                                        reply_to_id = _resolve_reply_to_id(message, d)
                                        try:
                                            fwded_msg = await _send_bot_media_album(
                                                d, bot_media,
                                                base_text=message.raw_text or message.text or "",
                                                reply_to=reply_to_id,
                                            )
                                            if fwded_msg is not None:
                                                st.stored[event_uid][d] = fwded_msg
                                                fwded_id = _extract_msg_id(fwded_msg)
                                                if fwded_id is not None:
                                                    st.add_post_mapping(
                                                        src, message.id, d, fwded_id
                                                    )
                                                message_sent = True
                                        except Exception as e:
                                            logging.error(f"❌ bot 媒体发送失败: {e}")
                        else:
                            # 普通单条消息
                            tm = await apply_plugins(message)
                            if not tm:
                                # 修复 Bug3：即使被过滤也要更新 offset
                                logging.info(f"🧩 普通单条消息被插件过滤 msg={message.id}，直接推进 offset")
                                _update_offset(
                                    forward, offset_key,
                                    message.id, sources,
                                )
                                continue

                            logging.info(f"🧩 普通单条发送模板 msg={message.id} tm={describe_nb_message(tm)}")

                            await _ensure_connected(client)

                            event_uid = st.EventUid(
                                st.DummyEvent(message.chat_id, message.id)
                            )
                            st.stored[event_uid] = {}

                            any_send_ok = False
                            for d in dest:
                                reply_to_id = _resolve_reply_to_id(message, d)
                                tm.reply_to = reply_to_id

                                try:
                                    fwded_msg = await send_message(d, tm)
                                    if fwded_msg is not None:
                                        any_send_ok = True
                                        st.stored[event_uid][d] = fwded_msg
                                        fwded_id = _extract_msg_id(fwded_msg)
                                        if fwded_id is not None:
                                            st.add_post_mapping(
                                                src, message.id, d, fwded_id
                                            )
                                    else:
                                        failed_destinations.append({
                                            "dest": d,
                                            "reason": "send_message returned None",
                                        })
                                        logging.warning(
                                            f"⚠️ 发送返回 None, "
                                            f"dest={d}, msg={message.id}"
                                        )
                                except Exception as e:
                                    failed_destinations.append({
                                        "dest": d,
                                        "reason": str(e),
                                    })
                                    logging.error(f"❌ 单条发送失败: {e}")

                            tm.clear()
                            message_sent = any_send_ok

                # 统一更新 offset，避免失败消息卡在当前 offset。
                last_id = message.id
                _update_offset(forward, offset_key, message.id, sources)
                logging.info(
                    f"💾 offset 已更新 src={src} offset_key={offset_key} new_offset={message.id} "
                    f"message_sent={message_sent} total_forwarded={total_forwarded}"
                )
                if not message_sent:
                    st.append_failed_forward_record(
                        mode="past-single",
                        source_chat_id=src,
                        source_message_id=message.id,
                        dest_chat_ids=list(dest),
                        reason="message send failed but offset advanced",
                        details={
                            "failed_destinations": failed_destinations,
                            "failed_log_file": FAILED_FORWARD_LOG_FILE_NAME,
                        },
                    )
                    logging.warning(
                        f"⚠️ 消息 {message.id} 发送失败, 仍推进 offset"
                    )

                if message_sent:
                    total_forwarded += 1
                    logging.info(f"✅ 消息处理完成 msg={message.id} total_forwarded={total_forwarded}")

                # 评论转发（与合并模式互斥）
                if (
                    forward.comments.enabled
                    and not forward.comments.merge_comment_media
                ):
                    try:
                        await _forward_comments_for_post(
                            client, src, message.id, forward
                        )
                    except Exception as e:
                        logging.error(
                            f"❌ 帖子 {message.id} 评论转发失败: {e}"
                        )

                # 检查限额
                if max_count > 0 and total_forwarded >= max_count:
                    logging.info(
                        f"📊 达到转发限额 {max_count}, 停止当前连接"
                    )
                    limit_reached = True
                    break

                delay_seconds = get_random_forward_delay()
                logging.info(
                    f"⏸️ 休息 {delay_seconds} 秒 (消息 {message.id})"
                )
                await asyncio.sleep(delay_seconds)

            except FloodWaitError as fwe:
                logging.warning(f"⛔ FloodWait: {fwe.seconds} 秒")
                await asyncio.sleep(fwe.seconds)
            except Exception as err:
                logging.exception(err)

        # 刷新剩余 grouped 缓冲
        if grouped_buffer:
            group_count = len(grouped_buffer)
            logging.info(
                f"📦 刷新剩余 {group_count} 个媒体组"
            )
            try:
                _, flushed_groups = await _flush_grouped_buffer(
                    client, src, dest, grouped_buffer, forward
                )
                total_forwarded += flushed_groups
            except Exception as e:
                logging.exception(f"🚨 刷新剩余组失败: {e}")

        if limit_reached:
            all_exhausted = False
            break

        logging.info(
            f"🏁 来源扫描完成 src={src} total_forwarded={total_forwarded} all_exhausted={all_exhausted}"
        )

    return (total_forwarded, all_exhausted)


# =====================================================================
#  主 forward_job
# =====================================================================

async def forward_job() -> None:
    clean_session_files()
    await load_async_plugins()

    if CONFIG.login.user_type != 1:
        logging.warning("⚠️ past 模式仅支持用户账号")
        return

    SESSION = get_SESSION()
    async with TelegramClient(
        SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH
    ) as client:
        config.from_to = await config.load_from_to(client, CONFIG.forwards)

        for forward in CONFIG.forwards:
            if not forward.use_this:
                continue
            forwarded, exhausted = await forward_with_limit(
                client, forward, max_count=0
            )
            logging.info(
                f"📊 连接 {forward.con_name or '(unnamed)'} 完成: "
                f"转发 {forwarded} 条, 来源{'已耗尽' if exhausted else '未耗尽'}"
            )
