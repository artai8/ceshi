"""Lease-based multi-tenant Telegram forwarding workers."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

os.environ.setdefault("NB_MULTITENANT", "1")

from telethon import TelegramClient, events, utils as telethon_utils
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, UserAlreadyParticipantError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, GetDiscussionMessageRequest
from telethon.tl.types import Channel, ChatInviteAlready, MessageService

from nb.plugins import PluginPipeline

from .models import AccountStatus, ForwardTask, ResolvedEndpoint, TaskMode, TaskStatus
from .repository import MongoRepository
from .security import SessionCipher
from .settings import Settings


SHANGHAI = ZoneInfo("Asia/Shanghai")


class TaskStopped(RuntimeError):
    pass


def _message_id(value) -> Optional[int]:
    if isinstance(value, list):
        value = value[0] if value else None
    return int(value.id) if value is not None and getattr(value, "id", None) else None


def _chunks(values: list, size: int = 10):
    for index in range(0, len(values), size):
        yield values[index:index + size]


@dataclass
class TaskRuntime:
    task: ForwardTask
    pipeline: PluginPipeline
    temp_dir: Path
    post_mapping: dict[tuple[int, int, int], int] = field(default_factory=dict)
    message_mapping: dict[tuple[int, int, int], int] = field(default_factory=dict)
    comment_destinations: list[int] = field(default_factory=list)
    source_discussion_id: Optional[int] = None

    def close(self) -> None:
        resolved = self.temp_dir.resolve()
        if self.temp_dir.name.startswith("nb-tenant-") and resolved.parent == Path(tempfile.gettempdir()).resolve():
            shutil.rmtree(resolved, ignore_errors=True)


class TenantRuntime:
    def __init__(
        self,
        settings: Settings,
        repository: MongoRepository,
        tenant_id: int,
        epoch: int,
    ) -> None:
        self.settings = settings
        self.repo = repository
        self.tenant_id = tenant_id
        self.epoch = epoch
        self.client: Optional[TelegramClient] = None
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=2000)
        self.operation_lock = asyncio.Lock()
        self.consumer: Optional[asyncio.Task] = None
        self.tasks: dict[str, TaskRuntime] = {}
        self.source_routes: dict[int, list[TaskRuntime]] = defaultdict(list)
        self.comment_routes: dict[int, list[tuple[TaskRuntime, int]]] = defaultdict(list)
        self._stopped = asyncio.Event()

    async def db(self, method, *args, **kwargs):
        return await asyncio.to_thread(method, *args, **kwargs)

    async def run(self) -> None:
        account = await self.db(self.repo.get_account, self.tenant_id)
        if account is None:
            return
        lease_heartbeat: Optional[asyncio.Task] = None
        try:
            if account["status"] == AccountStatus.DELETING.value:
                await self._delete_account(account)
                return
            session = SessionCipher(self.settings.session_encryption_key).decrypt(account["encrypted_session"])
            self.client = TelegramClient(
                StringSession(session),
                self.settings.api_id,
                self.settings.api_hash,
                sequential_updates=False,
            )
            await self.client.connect()
            if not await self.client.is_user_authorized():
                await self.db(
                    self.repo.set_account_status,
                    self.tenant_id,
                    AccountStatus.REAUTH_REQUIRED,
                    error="Telegram authorization expired",
                )
                return
            self._register_handlers()
            self.consumer = asyncio.create_task(self._consume())
            owner_task = asyncio.current_task()
            assert owner_task is not None
            lease_heartbeat = asyncio.create_task(self._renew_lease(owner_task))
            while not self._stopped.is_set():
                account = await self.db(self.repo.get_account, self.tenant_id)
                if account is None:
                    return
                if account["status"] == AccountStatus.DELETING.value:
                    async with self.operation_lock:
                        await self._delete_connected_account()
                    return
                async with self.operation_lock:
                    await self._reload_tasks()
                await self._schedule_due_jobs()
                await self._run_next_job()
                try:
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=min(self.settings.poll_seconds, self.settings.lease_seconds / 3),
                    )
                except asyncio.TimeoutError:
                    pass
        except (AuthKeyUnregisteredError, ValueError, RuntimeError) as exc:
            await self.db(
                self.repo.set_account_status,
                self.tenant_id,
                AccountStatus.REAUTH_REQUIRED,
                error=str(exc),
            )
        except Exception as exc:
            logging.exception("Tenant runtime crashed tenant=%s", self.tenant_id)
            await self.db(
                self.repo.set_account_status,
                self.tenant_id,
                AccountStatus.ACTIVE,
                error=f"worker error: {type(exc).__name__}",
            )
        finally:
            self._stopped.set()
            if lease_heartbeat:
                lease_heartbeat.cancel()
                await asyncio.gather(lease_heartbeat, return_exceptions=True)
            if self.consumer:
                self.consumer.cancel()
                await asyncio.gather(self.consumer, return_exceptions=True)
            for runtime in self.tasks.values():
                runtime.close()
            if self.client:
                await self.client.disconnect()
            await self.db(
                self.repo.release_lease,
                self.tenant_id,
                self.settings.worker_id,
                self.epoch,
            )

    async def _renew_lease(self, owner_task: asyncio.Task) -> None:
        try:
            while True:
                await asyncio.sleep(max(1, self.settings.lease_seconds / 3))
                renewed = await self.db(
                    self.repo.renew_lease,
                    self.tenant_id,
                    self.settings.worker_id,
                    self.epoch,
                    self.settings.lease_seconds,
                )
                if renewed:
                    continue
                logging.warning("Lease lost tenant=%s", self.tenant_id)
                self._stopped.set()
                owner_task.cancel()
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Lease renewal failed tenant=%s", self.tenant_id)
            self._stopped.set()
            owner_task.cancel()

    async def _delete_account(self, account: dict[str, Any]) -> None:
        client = None
        try:
            session = SessionCipher(self.settings.session_encryption_key).decrypt(
                account["encrypted_session"]
            )
            client = TelegramClient(
                StringSession(session),
                self.settings.api_id,
                self.settings.api_hash,
            )
            await client.connect()
            try:
                revoked = await client.log_out()
                if revoked is False:
                    raise RuntimeError("Telegram did not confirm authorization revocation")
            except AuthKeyUnregisteredError:
                pass
            await self.db(self.repo.hard_delete_account, self.tenant_id)
        except Exception as exc:
            await self.db(
                self.repo.set_account_delete_error,
                self.tenant_id,
                f"logout retry: {type(exc).__name__}",
            )
        finally:
            if client is not None:
                await client.disconnect()

    async def _delete_connected_account(self) -> None:
        assert self.client is not None
        try:
            try:
                revoked = await self.client.log_out()
                if revoked is False:
                    raise RuntimeError("Telegram did not confirm authorization revocation")
            except AuthKeyUnregisteredError:
                pass
            await self.db(self.repo.hard_delete_account, self.tenant_id)
        except Exception as exc:
            await self.db(
                self.repo.set_account_delete_error,
                self.tenant_id,
                f"logout retry: {type(exc).__name__}",
            )

    def _register_handlers(self) -> None:
        assert self.client is not None

        self.client.add_event_handler(
            lambda event: self._enqueue_event("new", event),
            events.NewMessage(),
        )
        self.client.add_event_handler(
            lambda event: self._enqueue_event("album", event),
            events.Album(),
        )
        self.client.add_event_handler(
            lambda event: self._enqueue_event("edited", event),
            events.MessageEdited(),
        )
        self.client.add_event_handler(
            lambda event: self._enqueue_event("deleted", event),
            events.MessageDeleted(),
        )

    async def _enqueue_event(self, kind: str, event) -> None:
        if self.queue.full():
            logging.warning(
                "Tenant queue applying backpressure tenant=%s kind=%s",
                self.tenant_id,
                kind,
            )
        await self.queue.put((kind, event))

    async def _consume(self) -> None:
        while True:
            kind, event = await self.queue.get()
            try:
                while True:
                    try:
                        async with self.operation_lock:
                            if kind == "new":
                                if getattr(event.message, "grouped_id", None) is None:
                                    await self._handle_messages(event.chat_id, [event.message])
                            elif kind == "album":
                                await self._handle_messages(event.chat_id, list(event.messages))
                            elif kind == "edited":
                                await self._handle_edit(event)
                            elif kind == "deleted":
                                await self._handle_delete(event)
                        break
                    except FloodWaitError as exc:
                        logging.warning("FloodWait tenant=%s seconds=%s", self.tenant_id, exc.seconds)
                        await asyncio.sleep(exc.seconds)
            except Exception as exc:
                logging.exception("Tenant event failed tenant=%s kind=%s", self.tenant_id, kind)
                await self._set_event_task_errors(event, exc)
            finally:
                self.queue.task_done()

    async def _set_event_task_errors(self, event, error: Exception) -> None:
        chat_id = getattr(event, "chat_id", None)
        tasks = {
            runtime.task.id: runtime.task
            for runtime in self.source_routes.get(chat_id, [])
        }
        tasks.update(
            {
                runtime.task.id: runtime.task
            for runtime, _ in self.comment_routes.get(chat_id, [])
            }
        )
        if not tasks and chat_id is None:
            tasks = {runtime.task.id: runtime.task for runtime in self.tasks.values()}
        for runtime in self.tasks.values():
            if runtime.task.id in tasks:
                runtime.task.status = TaskStatus.ERROR
        for task_id, task in tasks.items():
            await self.db(
                self.repo.set_task_error,
                self.tenant_id,
                task_id,
                f"{type(error).__name__}: {error}",
                expected_revision=task.revision,
                lease_epoch=self.epoch,
            )

    async def _reload_tasks(self) -> None:
        await self.db(self.repo.fence_tenant_tasks, self.tenant_id, self.epoch)
        values = await self.db(self.repo.list_runtime_tasks, self.tenant_id)
        validating = [task for task in values if task.status == TaskStatus.VALIDATING]
        for task in validating:
            await self._validate_task(task)
        if validating:
            values = await self.db(self.repo.list_runtime_tasks, self.tenant_id)

        keep = {task.id for task in values if task.status in {TaskStatus.ACTIVE, TaskStatus.RUNNING}}
        for task_id in list(self.tasks):
            if task_id not in keep:
                self.tasks.pop(task_id).close()

        for task in values:
            if task.status not in {TaskStatus.ACTIVE, TaskStatus.RUNNING}:
                continue
            existing = self.tasks.get(task.id)
            if existing and existing.task.revision == task.revision:
                continue
            if existing:
                existing.close()
            try:
                self.tasks[task.id] = await self._build_task_runtime(task)
            except Exception as exc:
                self.tasks.pop(task.id, None)
                await self.db(
                    self.repo.set_task_error,
                    self.tenant_id,
                    task.id,
                    str(exc),
                    expected_revision=task.revision,
                    lease_epoch=self.epoch,
                )

        self.source_routes.clear()
        self.comment_routes.clear()
        for runtime in self.tasks.values():
            if runtime.task.mode != TaskMode.LIVE:
                continue
            for endpoint in runtime.task.resolved_sources:
                self.source_routes[endpoint.peer_id].append(runtime)
                if runtime.task.comments.enabled:
                    try:
                        if runtime.task.comments.source_mode == "discussion":
                            discussion_id = runtime.source_discussion_id
                        else:
                            discussion_id = await self._discussion_group_id(endpoint.peer_id)
                    except Exception as exc:
                        await self.db(
                            self.repo.set_task_error,
                            self.tenant_id,
                            runtime.task.id,
                            f"Comment source validation failed: {exc}",
                            expected_revision=runtime.task.revision,
                            lease_epoch=self.epoch,
                        )
                        continue
                    if discussion_id:
                        self.comment_routes[discussion_id].append((runtime, endpoint.peer_id))

    async def _build_task_runtime(self, task: ForwardTask) -> TaskRuntime:
        temp_dir = Path(tempfile.mkdtemp(prefix="nb-tenant-"))
        plugin_config = task.plugins.model_copy(deep=True)
        if plugin_config.mark.check:
            if not task.watermark_asset_id:
                raise ValueError(f"Task {task.id} enables watermark without an uploaded PNG")
            data = await self.db(
                self.repo.get_watermark,
                self.tenant_id,
                task.id,
                task.watermark_asset_id,
            )
            watermark = temp_dir / "watermark.png"
            watermark.write_bytes(data)
            plugin_config.mark.image = str(watermark)
        pipeline = PluginPipeline(plugin_config, download_dir=str(temp_dir))
        await pipeline.initialize()
        comment_destinations = []
        source_discussion_id = None
        if task.comments.enabled and task.comments.source_mode == "discussion":
            source = await self._resolve_endpoint(
                str(task.comments.source_discussion_group),
                destination=False,
            )
            source_discussion_id = source.peer_id
        if task.comments.enabled and task.comments.dest_mode == "discussion":
            for raw in task.comments.dest_discussion_groups:
                endpoint = await self._resolve_endpoint(str(raw), destination=True)
                comment_destinations.append(endpoint.peer_id)
        return TaskRuntime(
            task=task,
            pipeline=pipeline,
            temp_dir=temp_dir,
            comment_destinations=comment_destinations,
            source_discussion_id=source_discussion_id,
        )

    async def _validate_task(self, task: ForwardTask) -> None:
        validated = None
        try:
            if not task.sources or not task.destinations:
                raise ValueError("At least one source and destination are required")
            sources = [await self._resolve_endpoint(value, destination=False) for value in task.sources]
            destinations = [await self._resolve_endpoint(value, destination=True) for value in task.destinations]
            if task.comments.enabled:
                if task.comments.source_mode == "discussion":
                    if task.comments.source_discussion_group is None:
                        raise ValueError("Comment source discussion group is required")
                    await self._resolve_endpoint(
                        str(task.comments.source_discussion_group),
                        destination=False,
                    )
                else:
                    for source in sources:
                        discussion_id = await self._discussion_group_id(source.peer_id)
                        if discussion_id is None:
                            raise ValueError(f"Source {source.raw} has no readable discussion group")
                        await self._resolve_endpoint(str(discussion_id), destination=False)
                if task.comments.dest_mode == "discussion":
                    if not task.comments.dest_discussion_groups:
                        raise ValueError("Comment destination discussion group is required")
                    for value in task.comments.dest_discussion_groups:
                        await self._resolve_endpoint(str(value), destination=True)
                else:
                    for destination in destinations:
                        discussion_id = await self._discussion_group_id(destination.peer_id)
                        if discussion_id is None:
                            raise ValueError(
                                f"Destination {destination.raw} has no writable discussion group"
                            )
                        await self._resolve_endpoint(str(discussion_id), destination=True)
            if task.plugins.mark.check and not task.watermark_asset_id:
                raise ValueError("Watermark plugin requires an uploaded PNG")
            if task.plugins.mark.check:
                await self.db(
                    self.repo.get_watermark,
                    self.tenant_id,
                    task.id,
                    task.watermark_asset_id,
                )
            if task.plugins.filter.check and task.plugins.filter.text.regex:
                for pattern in (
                    task.plugins.filter.text.blacklist + task.plugins.filter.text.whitelist
                ):
                    re.compile(pattern)
            if task.plugins.replace.check and task.plugins.replace.regex:
                for pattern in task.plugins.replace.text:
                    re.compile(pattern)
            pipeline = PluginPipeline(task.plugins.model_copy(deep=True))
            await pipeline.initialize()
            if task.pending_action == "run":
                status = TaskStatus.RUNNING if task.mode == TaskMode.PAST else TaskStatus.ACTIVE
            else:
                status = TaskStatus.PAUSED
            validated = await self.db(
                self.repo.set_task_validation_result,
                self.tenant_id,
                task.id,
                expected_revision=task.revision,
                lease_epoch=self.epoch,
                sources=[value.model_dump(mode="json") for value in sources],
                destinations=[value.model_dump(mode="json") for value in destinations],
                status=status,
            )
            if validated is None:
                return
            if task.pending_action == "run" and task.mode == TaskMode.PAST:
                await self.db(
                    self.repo.create_job,
                    self.tenant_id,
                    task.id,
                    kind="past",
                    task_revision=validated.revision,
                )
        except Exception as exc:
            await self.db(
                self.repo.set_task_error,
                self.tenant_id,
                task.id,
                str(exc),
                expected_revision=validated.revision if validated else task.revision,
                lease_epoch=self.epoch,
            )

    async def _resolve_endpoint(self, raw: str, *, destination: bool) -> ResolvedEndpoint:
        assert self.client is not None
        value: str | int = raw.strip()
        public_username = ""
        if re.fullmatch(r"-?\d+", str(value)):
            value = int(value)
        else:
            normalized = str(value)
            if normalized.startswith("@"):
                public_username = normalized[1:]
                value = public_username
            elif "t.me/" in normalized:
                parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
                path = parsed.path.strip("/")
                if path.startswith("+") or path.startswith("joinchat/"):
                    invite_hash = path.removeprefix("+").removeprefix("joinchat/")
                    invite = await self.client(CheckChatInviteRequest(invite_hash))
                    if not isinstance(invite, ChatInviteAlready):
                        raise ValueError("Private invite links must be joined manually first")
                    value = invite.chat
                elif path.startswith("c/"):
                    parts = path.split("/")
                    value = int(f"-100{parts[1]}")
                else:
                    public_username = path.split("/", 1)[0]
                    value = public_username
        entity = await self.client.get_entity(value)
        if public_username and isinstance(entity, Channel):
            try:
                await self.client(JoinChannelRequest(entity))
            except UserAlreadyParticipantError:
                pass
        if destination:
            await self._validate_destination_permission(entity)
        else:
            await self.client.get_messages(entity, limit=1)
        return ResolvedEndpoint(
            raw=raw,
            peer_id=telethon_utils.get_peer_id(entity),
            title=getattr(entity, "title", "") or "",
            username=getattr(entity, "username", "") or "",
        )

    async def _validate_destination_permission(self, entity) -> None:
        assert self.client is not None
        permissions = await self.client.get_permissions(entity, "me")
        if getattr(entity, "broadcast", False):
            admin = getattr(permissions, "is_admin", False) or getattr(permissions, "is_creator", False)
            can_post = getattr(permissions, "post_messages", False) or getattr(permissions, "is_creator", False)
            if not admin or not can_post:
                raise ValueError(f"No post permission in target {getattr(entity, 'title', entity)}")
        banned = getattr(permissions, "banned_rights", None)
        if banned and getattr(banned, "send_messages", False):
            raise ValueError(f"Sending is forbidden in target {getattr(entity, 'title', entity)}")

    async def _handle_messages(self, chat_id: int, messages: list) -> None:
        if chat_id in self.comment_routes:
            await self._handle_comments(chat_id, messages)
        for runtime in list(self.source_routes.get(chat_id, [])):
            if runtime.task.status not in {TaskStatus.ACTIVE, TaskStatus.RUNNING}:
                continue
            try:
                await self._forward_messages(runtime, chat_id, messages, live=True)
            except FloodWaitError:
                raise
            except Exception as exc:
                await self._mark_runtime_error(runtime, exc)

    async def _mark_runtime_error(self, runtime: TaskRuntime, error: Exception) -> None:
        logging.exception("Task runtime failed tenant=%s task=%s", self.tenant_id, runtime.task.id)
        runtime.task.status = TaskStatus.ERROR
        await self.db(
            self.repo.set_task_error,
            self.tenant_id,
            runtime.task.id,
            f"{type(error).__name__}: {error}",
            expected_revision=runtime.task.revision,
            lease_epoch=self.epoch,
        )

    async def _forward_messages(
        self,
        runtime: TaskRuntime,
        source_id: int,
        messages: list,
        *,
        live: bool,
    ) -> int:
        assert self.client is not None
        task = runtime.task
        values = [message for message in messages if not isinstance(message, MessageService)]
        if not values:
            return 0

        if task.comments.enabled and task.comments.merge_comment_media and len(values) == 1:
            if live:
                await asyncio.sleep(task.comments.merge_wait_seconds)
            values.extend(await self._collect_comment_media(source_id, values[0].id, task))

        bot_media = []
        if not self._hits_filter_blacklist(values[0], task):
            bot_media = await self._collect_bot_media(values[0], task)
        source_keys = {(getattr(item, "chat_id", source_id), item.id) for item in values}
        values.extend(
            item
            for item in bot_media
            if (getattr(item, "chat_id", None), item.id) not in source_keys
        )
        templates = (
            await runtime.pipeline.apply_group(
                values,
                skip_plugins=["filter"] if bot_media and task.bot_media.ignore_filter else None,
                fail_open=bool(bot_media and task.bot_media.force_forward_on_empty),
            )
            if len(values) > 1
            else []
        )
        if len(values) == 1:
            template = await runtime.pipeline.apply(values[0])
            templates = [template] if template else []
        if not templates:
            return 0

        sent_count = 0
        for destination in task.resolved_destinations:
            delivery_key = (
                f"{task.id}:{source_id}:"
                f"{','.join(str(message.id) for message in messages)}:{destination.peer_id}:new"
            )
            if not await self.db(
                self.repo.claim_delivery,
                delivery_key,
                self.tenant_id,
                task.id,
                source_peer_id=source_id,
                source_message_ids=[message.id for message in messages],
                destination_peer_id=destination.peer_id,
            ):
                continue
            reply_to = None
            reply_source_id = getattr(getattr(messages[0], "reply_to", None), "reply_to_msg_id", None)
            if reply_source_id:
                reply_to = runtime.message_mapping.get((source_id, destination.peer_id, reply_source_id))
            try:
                sent = await self._send_templates(task, destination.peer_id, templates, reply_to=reply_to)
                sent_id = _message_id(sent)
                sent_values = sent if isinstance(sent, list) else [sent]
                sent_ids = [int(value.id) for value in sent_values if getattr(value, "id", None)]
                message_map = {
                    str(original.id): sent_ids[min(index, len(sent_ids) - 1)]
                    for index, original in enumerate(messages)
                    if sent_ids
                }
                await self.db(
                    self.repo.mark_delivery_sent,
                    delivery_key,
                    sent_id,
                    message_map=message_map,
                )
            except Exception:
                await self.db(self.repo.release_delivery, delivery_key)
                raise
            if sent_id:
                sent_count += 1
                for original in messages:
                    runtime.message_mapping[(source_id, destination.peer_id, original.id)] = message_map[str(original.id)]
                runtime.post_mapping[(source_id, destination.peer_id, messages[0].id)] = sent_id
        for template in templates:
            template.clear()
        return sent_count

    async def _send_templates(self, task: ForwardTask, destination: int, templates: list, *, reply_to=None):
        assert self.client is not None
        if (
            task.show_forwarded_from
            and reply_to is None
            and all(not template.new_file for template in templates)
            and not any(
                getattr(getattr(task.plugins, plugin_id), "check", False)
                for plugin_id in ["ocr", "replace", "caption", "fmt", "mark", "spoiler", "inline"]
            )
        ):
            originals = [template.message for template in templates]
            return await self.client.forward_messages(destination, originals)

        media_templates = [template for template in templates if getattr(template.message, "media", None)]
        text_templates = [template for template in templates if not getattr(template.message, "media", None)]
        sent = []
        if media_templates:
            prefix = "\n\n".join(template.text for template in text_templates if template.text).strip()
            captions = [template.text or "" for template in media_templates]
            if prefix:
                captions[0] = f"{prefix}\n\n{captions[0]}".strip()
            for group in _chunks(media_templates):
                files = [template.new_file or template.message.media for template in group]
                group_captions = captions[:len(group)]
                captions = captions[len(group):]
                result = await self.client.send_file(
                    destination,
                    files,
                    caption=group_captions,
                    reply_to=reply_to,
                    buttons=group[0].reply_markup if len(group) == 1 else None,
                )
                sent.extend(result if isinstance(result, list) else [result])
                reply_to = None
        else:
            for template in templates:
                if not template.text:
                    continue
                result = await self.client.send_message(
                    destination,
                    template.text,
                    reply_to=reply_to,
                    buttons=template.reply_markup,
                    link_preview=False,
                )
                sent.append(result)
                reply_to = None
        return sent

    async def _collect_bot_media(self, message, task: ForwardTask) -> list:
        assert self.client is not None
        settings = task.bot_media
        if not settings.enabled:
            return []

        links = self._start_links(message, settings)
        discussion_root = None
        discussion_comments = []
        if getattr(message, "post", False):
            discussion_root, discussion_comments = await self._discussion_context(message, settings.recent_limit)
            for comment in discussion_comments:
                links.extend(self._start_links(comment, settings))

        keyword = self._extract_comment_keyword(message.raw_text or message.text or "", settings)
        if keyword and discussion_root is not None and settings.auto_comment_trigger_enabled:
            latest = await self.client.get_messages(discussion_root.chat_id, limit=1)
            last_id = latest[0].id if latest else 0
            await self.client.send_message(discussion_root.chat_id, keyword, reply_to=discussion_root.id)
            responses = await self._poll_bot_messages(discussion_root.chat_id, last_id, settings)
            for response in responses:
                links.extend(self._start_links(response, settings))

        if not links and discussion_root is not None and settings.comment_keyword_from_comments_enabled:
            common_keyword = self._common_comment_keyword(discussion_comments)
            if common_keyword:
                latest = await self.client.get_messages(discussion_root.chat_id, limit=1)
                last_id = latest[0].id if latest else 0
                await self.client.send_message(discussion_root.chat_id, common_keyword, reply_to=discussion_root.id)
                responses = await self._poll_bot_messages(discussion_root.chat_id, last_id, settings)
                for response in responses:
                    links.extend(self._start_links(response, settings))

        collected = await self._collect_from_start_links(links, settings)
        if collected or not settings.enable_keyword_trigger:
            return collected

        text = (message.raw_text or message.text or "").strip()
        for username in re.findall(r"(?<![\w])@([A-Za-z0-9_]+)", text)[:1]:
            if any(blocked.lower() in username.lower() for blocked in settings.tme_link_blacklist):
                continue
            try:
                entity = await self.client.get_entity(username)
                if not getattr(entity, "bot", False):
                    continue
                latest = await self.client.get_messages(entity, limit=1)
                last_id = latest[0].id if latest else 0
                await self.client.send_message(entity, text)
                responses = await self._poll_bot_messages(entity, last_id, settings)
                response_links = []
                for response in responses:
                    response_links.extend(self._start_links(response, settings))
                collected = await self._collect_from_start_links(response_links, settings)
                if collected:
                    return collected
            except FloodWaitError:
                raise
            except Exception as exc:
                logging.info("Bot media lookup skipped tenant=%s bot=%s error=%s", self.tenant_id, username, type(exc).__name__)
        return []

    @staticmethod
    def _hits_filter_blacklist(message, task: ForwardTask) -> bool:
        config = task.plugins.filter
        if not config.check or not config.text.blacklist:
            return False
        text = message.raw_text or message.text or ""
        if not config.text.case_sensitive:
            text = text.lower()
        for pattern in config.text.blacklist:
            value = pattern if config.text.case_sensitive or config.text.regex else pattern.lower()
            if config.text.regex:
                if re.search(value, text):
                    return True
            elif value in text:
                return True
        return False

    async def _collect_from_start_links(self, links: list[tuple[str, str]], settings) -> list:
        assert self.client is not None
        collected = []
        seen_links = set()
        for username, start_param in links:
            key = (username.lower(), start_param)
            if key in seen_links:
                continue
            seen_links.add(key)
            try:
                entity = await self.client.get_entity(username)
                if not getattr(entity, "bot", False):
                    continue
                latest = await self.client.get_messages(entity, limit=1)
                last_id = latest[0].id if latest else 0
                command = f"/start {start_param}" if start_param else "/start"
                await self.client.send_message(entity, command)
                responses = await self._poll_bot_messages(entity, last_id, settings)
                pages = 1
                while settings.enable_pagination and responses and pages < settings.max_pages:
                    last = responses[-1]
                    button = self._pagination_button(last, settings)
                    if button is None:
                        break
                    before = last.id
                    await button.click()
                    next_page = await self._poll_bot_messages(entity, before, settings)
                    if not next_page:
                        break
                    responses.extend(next_page)
                    pages += 1
                collected.extend(item for item in responses if getattr(item, "media", None))
            except FloodWaitError:
                raise
            except Exception as exc:
                logging.info(
                    "Bot media start link skipped tenant=%s bot=%s error=%s",
                    self.tenant_id,
                    username,
                    type(exc).__name__,
                )
        deduplicated = {
            (getattr(item, "chat_id", None), item.id): item
            for item in collected
        }
        return list(deduplicated.values())

    async def _discussion_context(self, message, limit: int) -> tuple[Any, list]:
        assert self.client is not None
        try:
            result = await self.client(
                GetDiscussionMessageRequest(peer=message.chat_id, msg_id=message.id)
            )
            root = result.messages[0] if result.messages else None
            if root is None:
                return None, []
            comments = [
                item
                async for item in self.client.iter_messages(
                    root.chat_id,
                    reply_to=root.id,
                    reverse=True,
                    limit=limit,
                )
            ]
            return root, comments
        except FloodWaitError:
            raise
        except Exception:
            return None, []

    @staticmethod
    def _start_links(message, settings) -> list[tuple[str, str]]:
        values = []
        text = message.raw_text or message.text or ""
        values.extend(
            re.findall(
                r"(?:https?://)?t\.me/([A-Za-z0-9_]+)(?:\?start=([A-Za-z0-9_-]+))?",
                text,
                flags=re.IGNORECASE,
            )
        )
        for entity in getattr(message, "entities", None) or []:
            url = getattr(entity, "url", None)
            if url:
                values.extend(
                    re.findall(
                        r"(?:https?://)?t\.me/([A-Za-z0-9_]+)(?:\?start=([A-Za-z0-9_-]+))?",
                        url,
                        flags=re.IGNORECASE,
                    )
                )
        for row in getattr(message, "buttons", None) or []:
            for button in row:
                url = getattr(button, "url", None)
                if url:
                    values.extend(
                        re.findall(
                            r"(?:https?://)?t\.me/([A-Za-z0-9_]+)(?:\?start=([A-Za-z0-9_-]+))?",
                            url,
                            flags=re.IGNORECASE,
                        )
                    )
        blacklist = [value.lower() for value in settings.tme_link_blacklist]
        return [
            (username, start_param)
            for username, start_param in values
            if not any(
                blocked and blocked in f"{username}?start={start_param}".lower()
                for blocked in blacklist
            )
        ]

    @staticmethod
    def _extract_comment_keyword(text: str, settings) -> Optional[str]:
        for prefix in settings.comment_keyword_prefixes:
            start = text.find(prefix)
            if start < 0:
                continue
            start += len(prefix)
            ends = [text.find(suffix, start) for suffix in settings.comment_keyword_suffixes]
            ends = [value for value in ends if value >= 0]
            if not ends:
                continue
            keyword = text[start:min(ends)].strip(" \t\r\n:：,，。.!！?？'\"“”‘’[]【】()（）")
            if keyword:
                return keyword
        return None

    @staticmethod
    def _common_comment_keyword(comments: list) -> Optional[str]:
        counts: dict[str, int] = {}
        for comment in comments:
            text = (comment.raw_text or comment.text or "").strip()
            if text:
                counts[text] = counts.get(text, 0) + 1
        for text, count in counts.items():
            if count >= 3:
                return text
        return None

    async def _poll_bot_messages(self, entity, min_id: int, settings) -> list:
        assert self.client is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.wait_timeout
        quiet_period = max(0.5, min(2.0, settings.poll_interval * 2))
        collected: dict[int, Any] = {}
        last_change = loop.time()
        while loop.time() < deadline:
            changed = False
            async for item in self.client.iter_messages(entity, min_id=min_id, reverse=True):
                if getattr(item, "out", False) or item.id in collected:
                    continue
                collected[item.id] = item
                changed = True
            if changed:
                last_change = loop.time()
            if collected and loop.time() - last_change >= quiet_period:
                break
            await asyncio.sleep(settings.poll_interval)
        return [collected[key] for key in sorted(collected)]

    @staticmethod
    def _pagination_button(message, settings):
        rows = getattr(message, "buttons", None) or []
        ignored = [value.lower() for value in settings.pagination_ignore_keywords]
        keywords = [value.lower() for value in settings.pagination_keywords]
        if not keywords:
            keywords = [
                "下一页", "下页", "继续", "更多", "查看全部", "获取全部", "全部获取",
                "next", "more", "next page", "continue", "get all", "all files", "➡", ">",
            ]
        for row in rows:
            for button in row:
                text = (getattr(button, "text", "") or "").strip().lower()
                compact = text.replace(" ", "")
                if any(
                    value and (value in text or value.replace(" ", "") in compact)
                    for value in ignored
                ):
                    continue
                if settings.pagination_mode == "any" or any(
                    value in text or value.replace(" ", "") in compact
                    for value in keywords
                ):
                    return button
        return None

    async def _discussion_group_id(self, channel_id: int) -> Optional[int]:
        assert self.client is not None
        try:
            result = await self.client(GetFullChannelRequest(channel_id))
            linked = getattr(result.full_chat, "linked_chat_id", None)
            if linked:
                entity = await self.client.get_entity(linked)
                return telethon_utils.get_peer_id(entity)
        except FloodWaitError:
            raise
        except Exception:
            return None
        return None

    async def _collect_comment_media(self, channel_id: int, post_id: int, task: ForwardTask) -> list:
        assert self.client is not None
        try:
            result = await self.client(GetDiscussionMessageRequest(peer=channel_id, msg_id=post_id))
            root = result.messages[0] if result.messages else None
            if root is None:
                return []
            values = []
            async for comment in self.client.iter_messages(root.chat_id, reply_to=root.id, reverse=True):
                sender = None
                if task.comments.skip_bot_comments or task.comments.skip_admin_comments:
                    sender = await comment.get_sender()
                if task.comments.skip_bot_comments and getattr(sender, "bot", False):
                    continue
                if task.comments.skip_admin_comments and sender is not None:
                    try:
                        permissions = await self.client.get_permissions(root.chat_id, sender)
                        if getattr(permissions, "is_admin", False) or getattr(
                            permissions, "is_creator", False
                        ):
                            continue
                    except Exception:
                        pass
                if getattr(comment, "media", None):
                    values.append(comment)
                elif task.comments.include_text_comments and not task.comments.only_media:
                    values.append(comment)
            return values
        except FloodWaitError:
            raise
        except Exception:
            return []

    async def _handle_comment(self, discussion_id: int, message) -> None:
        await self._handle_comments(discussion_id, [message])

    async def _handle_comments(self, discussion_id: int, messages: list) -> None:
        assert self.client is not None
        if not messages:
            return
        primary = messages[0]
        top_id = getattr(getattr(primary, "reply_to", None), "reply_to_top_id", None)
        top_id = top_id or getattr(getattr(primary, "reply_to", None), "reply_to_msg_id", None)
        if not top_id:
            return
        root = await self.client.get_messages(discussion_id, ids=top_id)
        source_post_id = getattr(getattr(root, "fwd_from", None), "channel_post", None)
        if not source_post_id:
            return
        for runtime, source_channel_id in self.comment_routes.get(discussion_id, []):
            if runtime.task.status not in {TaskStatus.ACTIVE, TaskStatus.RUNNING}:
                continue
            try:
                await self._forward_comments(
                    runtime,
                    source_channel_id,
                    discussion_id,
                    int(source_post_id),
                    messages,
                )
            except FloodWaitError:
                raise
            except Exception as exc:
                await self._mark_runtime_error(runtime, exc)

    async def _forward_comment(
        self,
        runtime: TaskRuntime,
        source_channel_id: int,
        discussion_id: int,
        source_post_id: int,
        message,
    ) -> None:
        await self._forward_comments(
            runtime,
            source_channel_id,
            discussion_id,
            source_post_id,
            [message],
        )

    async def _forward_comments(
        self,
        runtime: TaskRuntime,
        source_channel_id: int,
        discussion_id: int,
        source_post_id: int,
        messages: list,
    ) -> None:
        assert self.client is not None
        if not messages:
            return
        cfg = runtime.task.comments
        primary = messages[0]
        sender = await primary.get_sender()
        if cfg.skip_bot_comments and getattr(sender, "bot", False):
            return
        has_media = any(getattr(message, "media", None) for message in messages)
        if cfg.only_media and not has_media:
            return
        if not cfg.include_text_comments and not has_media:
            return
        if cfg.skip_admin_comments and sender is not None:
            try:
                permissions = await self.client.get_permissions(discussion_id, sender)
                if getattr(permissions, "is_admin", False) or getattr(
                    permissions, "is_creator", False
                ):
                    return
            except Exception:
                pass

        bot_media = []
        if not self._hits_filter_blacklist(primary, runtime.task):
            bot_media = await self._collect_bot_media(primary, runtime.task)
        values = list(messages)
        source_keys = {
            (getattr(message, "chat_id", discussion_id), message.id)
            for message in messages
        }
        values.extend(
            item
            for item in bot_media
            if (getattr(item, "chat_id", None), item.id) not in source_keys
        )
        if len(values) > 1:
            templates = await runtime.pipeline.apply_group(
                values,
                skip_plugins=["filter"] if runtime.task.bot_media.ignore_filter else None,
                fail_open=runtime.task.bot_media.force_forward_on_empty,
            )
        else:
            template = await runtime.pipeline.apply(primary)
            templates = [template] if template else []
        if not templates:
            return

        targets: list[tuple[int, Optional[int], int]] = []
        if cfg.dest_mode == "discussion":
            targets.extend((peer_id, None, peer_id) for peer_id in runtime.comment_destinations)
        else:
            for destination in runtime.task.resolved_destinations:
                if cfg.post_mapping_mode == "manual":
                    raw_dest_post = cfg.manual_post_mapping.get(
                        f"{source_channel_id}:{source_post_id}"
                    ) or cfg.manual_post_mapping.get(str(source_post_id))
                    dest_post_id = int(raw_dest_post) if raw_dest_post else None
                else:
                    dest_post_id = runtime.post_mapping.get(
                        (source_channel_id, destination.peer_id, source_post_id)
                    )
                    if not dest_post_id:
                        dest_post_id = await self.db(
                            self.repo.find_sent_message,
                            self.tenant_id,
                            runtime.task.id,
                            source_channel_id,
                            source_post_id,
                            destination.peer_id,
                        )
                if not dest_post_id:
                    continue
                try:
                    discussion = await self.client(
                        GetDiscussionMessageRequest(peer=destination.peer_id, msg_id=dest_post_id)
                    )
                    dest_root = discussion.messages[0]
                    targets.append((dest_root.chat_id, dest_root.id, destination.peer_id))
                except FloodWaitError:
                    raise
                except Exception as exc:
                    raise RuntimeError(
                        f"Destination discussion lookup failed for task {runtime.task.id}"
                    ) from exc

        for target_chat, reply_to, destination_key in targets:
            source_ids = [message.id for message in messages]
            delivery_key = (
                f"{runtime.task.id}:{discussion_id}:"
                f"{','.join(str(value) for value in source_ids)}:"
                f"{destination_key}:comment"
            )
            if not await self.db(
                self.repo.claim_delivery,
                delivery_key,
                self.tenant_id,
                runtime.task.id,
                source_peer_id=source_channel_id,
                source_message_ids=source_ids,
                destination_peer_id=destination_key,
            ):
                continue
            try:
                sent = await self._send_templates(
                    runtime.task,
                    target_chat,
                    templates,
                    reply_to=reply_to,
                )
                await self.db(self.repo.mark_delivery_sent, delivery_key, _message_id(sent))
            except FloodWaitError:
                await self.db(self.repo.release_delivery, delivery_key)
                raise
            except Exception:
                await self.db(self.repo.release_delivery, delivery_key)
                raise
        for template in templates:
            template.clear()

    async def _forward_historical_comments(
        self,
        runtime: TaskRuntime,
        source_channel_id: int,
        post_message,
    ) -> None:
        assert self.client is not None
        if not runtime.task.comments.enabled or runtime.task.comments.merge_comment_media:
            return
        root = None
        try:
            discussion = await self.client(
                GetDiscussionMessageRequest(peer=source_channel_id, msg_id=post_message.id)
            )
            root = discussion.messages[0] if discussion.messages else None
        except FloodWaitError:
            raise
        except Exception:
            pass
        if root is None and runtime.source_discussion_id is not None:
            async for candidate in self.client.iter_messages(
                runtime.source_discussion_id,
                limit=runtime.task.bot_media.recent_limit,
            ):
                channel_post = getattr(getattr(candidate, "fwd_from", None), "channel_post", None)
                if channel_post == post_message.id:
                    root = candidate
                    break
        if root is None:
            return
        async for comment in self.client.iter_messages(
            root.chat_id,
            reply_to=root.id,
            reverse=True,
        ):
            await self._forward_comment(
                runtime,
                source_channel_id,
                root.chat_id,
                post_message.id,
                comment,
            )

    async def _handle_edit(self, event) -> None:
        for runtime in self.source_routes.get(event.chat_id, []):
            if runtime.task.status not in {TaskStatus.ACTIVE, TaskStatus.RUNNING}:
                continue
            try:
                if runtime.task.live.delete_on_edit and event.raw_text == runtime.task.live.delete_on_edit:
                    await self._delete_mapped(runtime, event.chat_id, [event.id])
                    continue
                template = await runtime.pipeline.apply(event.message)
                if not template:
                    continue
                for destination in runtime.task.resolved_destinations:
                    mapped = runtime.message_mapping.get(
                        (event.chat_id, destination.peer_id, event.id)
                    )
                    if not mapped:
                        mapped = await self.db(
                            self.repo.find_sent_message,
                            self.tenant_id,
                            runtime.task.id,
                            event.chat_id,
                            event.id,
                            destination.peer_id,
                        )
                    if mapped:
                        await self.client.edit_message(destination.peer_id, mapped, template.text)
                template.clear()
            except FloodWaitError:
                raise
            except Exception as exc:
                await self._mark_runtime_error(runtime, exc)

    async def _handle_delete(self, event) -> None:
        for runtime in self.tasks.values():
            if runtime.task.status not in {TaskStatus.ACTIVE, TaskStatus.RUNNING}:
                continue
            if runtime.task.live.delete_sync:
                try:
                    await self._delete_mapped(
                        runtime,
                        getattr(event, "chat_id", 0),
                        list(event.deleted_ids),
                    )
                except FloodWaitError:
                    raise
                except Exception as exc:
                    await self._mark_runtime_error(runtime, exc)

    async def _delete_mapped(self, runtime: TaskRuntime, source_id: int, message_ids: list[int]) -> None:
        assert self.client is not None
        for source_message_id in message_ids:
            for destination in runtime.task.resolved_destinations:
                mapped = runtime.message_mapping.pop(
                    (source_id, destination.peer_id, source_message_id),
                    None,
                )
                if not mapped:
                    mapped = await self.db(
                        self.repo.find_sent_message,
                        self.tenant_id,
                        runtime.task.id,
                        source_id,
                        source_message_id,
                        destination.peer_id,
                    )
                if mapped:
                    await self.client.delete_messages(destination.peer_id, mapped)

    async def _schedule_due_jobs(self) -> None:
        now = datetime.now(SHANGHAI)
        for runtime in self.tasks.values():
            task = runtime.task
            if task.mode != TaskMode.SCHEDULE or task.status != TaskStatus.ACTIVE:
                continue
            if now.strftime("%H:%M") < task.schedule.run_time:
                continue
            key = f"schedule:{task.id}:{now.date().isoformat()}"
            hour, minute = (int(value) for value in task.schedule.run_time.split(":"))
            scheduled_at = now.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            ).astimezone(timezone.utc)
            await self.db(
                self.repo.create_job,
                self.tenant_id,
                task.id,
                kind="schedule",
                job_key=key,
                scheduled_at=scheduled_at,
                task_revision=task.revision,
                replace_cancelled=True,
            )

    async def _run_next_job(self) -> None:
        job = await self.db(
            self.repo.claim_job,
            self.tenant_id,
            self.settings.worker_id,
            self.epoch,
        )
        if not job:
            return
        task = await self.db(self.repo.get_task, self.tenant_id, job["task_id"])
        if task is None:
            await self.db(
                self.repo.finish_job,
                job["_id"],
                worker_id=self.settings.worker_id,
                lease_epoch=self.epoch,
                error="Task not found",
            )
            return
        if job.get("task_revision") is not None and job["task_revision"] != task.revision:
            await self.db(
                self.repo.cancel_job,
                job["_id"],
                worker_id=self.settings.worker_id,
                lease_epoch=self.epoch,
            )
            return
        runtime = self.tasks.get(task.id)
        if runtime is None or runtime.task.revision != task.revision:
            if runtime is not None:
                runtime.close()
            runtime = await self._build_task_runtime(task)
            if task.id in self.tasks:
                self.tasks[task.id] = runtime
        try:
            limit = task.schedule.daily_limit if job["kind"] == "schedule" else 0
            await self._run_history(runtime, limit=limit)
            finished = await self.db(
                self.repo.finish_job,
                job["_id"],
                worker_id=self.settings.worker_id,
                lease_epoch=self.epoch,
            )
            if not finished:
                return
            await self.db(
                self.repo.set_task_status,
                self.tenant_id,
                task.id,
                TaskStatus.ACTIVE if task.mode == TaskMode.SCHEDULE else TaskStatus.COMPLETED,
                expected_revision=task.revision,
                lease_epoch=self.epoch,
            )
        except TaskStopped:
            await self.db(
                self.repo.cancel_job,
                job["_id"],
                worker_id=self.settings.worker_id,
                lease_epoch=self.epoch,
            )
        except Exception as exc:
            finished = await self.db(
                self.repo.finish_job,
                job["_id"],
                worker_id=self.settings.worker_id,
                lease_epoch=self.epoch,
                error=str(exc),
            )
            if finished:
                await self.db(
                    self.repo.set_task_error,
                    self.tenant_id,
                    task.id,
                    str(exc),
                    expected_revision=task.revision,
                    lease_epoch=self.epoch,
                )
        finally:
            if task.id not in self.tasks:
                runtime.close()

    async def _run_history(self, runtime: TaskRuntime, *, limit: int) -> None:
        assert self.client is not None
        task = runtime.task
        count = 0
        offsets = dict(task.offsets)
        for source in task.resolved_sources:
            current_group: list = []
            current_group_id = None

            async def flush() -> None:
                nonlocal count, current_group, current_group_id
                if not current_group:
                    return
                current = await self.db(self.repo.get_task, self.tenant_id, task.id)
                if current is None or current.status not in {TaskStatus.ACTIVE, TaskStatus.RUNNING}:
                    raise TaskStopped(task.id)
                while True:
                    try:
                        async with self.operation_lock:
                            forwarded = await self._forward_messages(
                                runtime,
                                source.peer_id,
                                current_group,
                                live=False,
                            )
                            for post_message in current_group:
                                if getattr(post_message, "post", False):
                                    await self._forward_historical_comments(
                                        runtime,
                                        source.peer_id,
                                        post_message,
                                    )
                        break
                    except FloodWaitError as exc:
                        logging.warning(
                            "History FloodWait tenant=%s seconds=%s",
                            self.tenant_id,
                            exc.seconds,
                        )
                        await asyncio.sleep(exc.seconds)
                offsets[str(source.peer_id)] = current_group[-1].id
                if forwarded:
                    count += 1
                current_group = []
                current_group_id = None

            async for message in self.client.iter_messages(
                source.peer_id,
                reverse=True,
                offset_id=offsets.get(str(source.peer_id), 0),
            ):
                if isinstance(message, MessageService):
                    continue
                grouped_id = getattr(message, "grouped_id", None)
                if current_group and grouped_id != current_group_id:
                    await flush()
                    if limit and count >= limit:
                        break
                current_group_id = grouped_id
                current_group.append(message)
                if grouped_id is None:
                    await flush()
                    if limit and count >= limit:
                        break
                if task.past.delay:
                    await asyncio.sleep(task.past.delay)
            if not limit or count < limit:
                await flush()
            updated = await self.db(
                self.repo.update_task_offsets,
                self.tenant_id,
                task.id,
                offsets,
                expected_revision=task.revision,
                lease_epoch=self.epoch,
            )
            if not updated:
                raise TaskStopped(task.id)
            if limit and count >= limit:
                break


class Worker:
    def __init__(self, settings: Settings, repository: MongoRepository) -> None:
        self.settings = settings
        self.repo = repository
        self.runtimes: dict[int, asyncio.Task] = {}

    async def run(self) -> None:
        await asyncio.to_thread(self.repo.ensure_indexes)
        while True:
            for tenant_id, task in list(self.runtimes.items()):
                if task.done():
                    await asyncio.gather(task, return_exceptions=True)
                    self.runtimes.pop(tenant_id, None)
            capacity = self.settings.worker_capacity - len(self.runtimes)
            if capacity > 0:
                candidates = await asyncio.to_thread(self.repo.claimable_tenant_ids)
                for tenant_id in candidates:
                    if capacity <= 0:
                        break
                    if tenant_id in self.runtimes:
                        continue
                    lease = await asyncio.to_thread(
                        self.repo.claim_lease,
                        tenant_id,
                        self.settings.worker_id,
                        self.settings.lease_seconds,
                    )
                    if lease is None:
                        continue
                    runtime = TenantRuntime(self.settings, self.repo, tenant_id, int(lease["epoch"]))
                    self.runtimes[tenant_id] = asyncio.create_task(runtime.run())
                    capacity -= 1
            await asyncio.sleep(self.settings.poll_seconds)


async def async_main() -> None:
    settings = Settings.for_worker()
    repository = MongoRepository(settings.mongo_uri, settings.mongo_db)
    await Worker(settings, repository).run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
