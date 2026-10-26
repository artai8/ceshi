"""Private-chat control bot. It never handles or forwards channel messages."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import qrcode
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import KeyboardButtonWebView

from .repository import MongoRepository
from .security import SessionCipher
from .settings import Settings


@dataclass
class LoginAttempt:
    client: TelegramClient
    created_at: float
    phone: str = ""
    waiting_password: bool = False


def _get_proxy():
        if os.getenv("PROXY_ENABLED", "1") == "0":
                    return None
                return (
                            os.getenv("PROXY_TYPE", "socks5"),
                            os.getenv("PROXY_HOST", "127.0.0.1"),
                            int(os.getenv("PROXY_PORT", "7928")),
                )


    class ControlBot:
    def __init__(self, settings: Settings, repository: MongoRepository) -> None:
        self.settings = settings
        self.repo = repository
        self.cipher = SessionCipher(settings.session_encryption_key)
        self.bot = TelegramClient(StringSession(), settings.api_id, settings.api_hash, proxy=_get_proxy())
        self.attempts: dict[int, LoginAttempt] = {}
        self.rate_limits: dict[int, list[float]] = {}

    async def run(self) -> None:
        self.repo.ensure_indexes()
        await self.bot.start(bot_token=self.settings.bot_token)
        self._register_handlers()
        logging.info("Control bot started; only private command handlers are registered")
        notifier = asyncio.create_task(self._notify_status_changes())
        try:
            await self.bot.run_until_disconnected()
        finally:
            notifier.cancel()
            await asyncio.gather(notifier, return_exceptions=True)

    def _register_handlers(self) -> None:
        private = lambda event: bool(event.is_private)
        self.bot.add_event_handler(self.start, events.NewMessage(pattern=r"^/start$", func=private))
        self.bot.add_event_handler(self.login, events.NewMessage(pattern=r"^/login$", func=private))
        self.bot.add_event_handler(self.phone, events.NewMessage(pattern=r"^/phone(?:\s+(.+))?$", func=private))
        self.bot.add_event_handler(self.code, events.NewMessage(pattern=r"^/code(?:\s+(.+))?$", func=private))
        self.bot.add_event_handler(self.password, events.NewMessage(pattern=r"^/password(?:\s+(.+))?$", func=private))
        self.bot.add_event_handler(self.app, events.NewMessage(pattern=r"^/app$", func=private))
        self.bot.add_event_handler(self.web, events.NewMessage(pattern=r"^/web$", func=private))
        self.bot.add_event_handler(self.status, events.NewMessage(pattern=r"^/status$", func=private))
        self.bot.add_event_handler(self.logout, events.NewMessage(pattern=r"^/logout$", func=private))

    async def start(self, event) -> None:
        sender = await event.get_sender()
        name = " ".join(part for part in [sender.first_name, sender.last_name] if part)
        self.repo.upsert_tenant(sender.id, username=sender.username or "", display_name=name)
        await event.respond(
            "欢迎使用 NB 多租户转发。\n\n"
            "/login 绑定当前 Telegram 用户账号\n"
            "/app 打开 Mini App\n"
            "/web 获取浏览器后台链接\n"
            "/status 查看账号状态\n"
            "/logout 解绑并删除全部数据"
        )

    def _allow_login(self, tenant_id: int) -> bool:
        now = time.monotonic()
        attempts = [value for value in self.rate_limits.get(tenant_id, []) if now - value < 900]
        if len(attempts) >= 5:
            self.rate_limits[tenant_id] = attempts
            return False
        attempts.append(now)
        self.rate_limits[tenant_id] = attempts
        return True

    async def _new_client(self) -> TelegramClient:
        client = TelegramClient(StringSession(), self.settings.api_id, self.settings.api_hash, proxy=_get_proxy())
        await client.connect()
        return client

    async def _replace_attempt(self, tenant_id: int, attempt: LoginAttempt) -> None:
        previous = self.attempts.pop(tenant_id, None)
        if previous:
            await previous.client.disconnect()
        self.attempts[tenant_id] = attempt

    async def login(self, event) -> None:
        tenant_id = int(event.sender_id)
        if not self._allow_login(tenant_id):
            await event.respond("登录尝试过于频繁，请 15 分钟后再试。")
            return
        if self.repo.get_account(tenant_id):
            await event.respond("当前账号已经绑定。如需重绑，请先使用 /logout。")
            return
        client = await self._new_client()
        await self._replace_attempt(tenant_id, LoginAttempt(client=client, created_at=time.monotonic()))
        try:
            qr_login = await client.qr_login()
            image = qrcode.make(qr_login.url)
            buffer = io.BytesIO()
            buffer.name = "telegram-login.png"
            image.save(buffer, format="PNG")
            buffer.seek(0)
            await event.respond(
                "请使用另一个已登录设备扫描二维码。二维码短时有效。\n"
                "无法扫码时发送：/phone +国家码手机号",
                file=buffer,
            )
            asyncio.create_task(self._wait_for_qr(tenant_id, qr_login, client, event.chat_id))
        except Exception as exc:
            logging.warning("QR login setup failed for tenant=%s: %s", tenant_id, type(exc).__name__)
            await event.respond("二维码生成失败，请使用 /phone +国家码手机号。")

    async def _wait_for_qr(
        self,
        tenant_id: int,
        qr_login,
        client: TelegramClient,
        chat_id: int,
    ) -> None:
        try:
            await qr_login.wait(timeout=120)
            attempt = self.attempts.get(tenant_id)
            if attempt is None or attempt.client is not client:
                return
            await self._complete_login(tenant_id, attempt.client, chat_id)
        except asyncio.TimeoutError:
            if self.attempts.get(tenant_id) and self.attempts[tenant_id].client is client:
                await self.bot.send_message(chat_id, "二维码已过期，请重新发送 /login。")
            await self._discard_attempt(tenant_id, client)
        except SessionPasswordNeededError:
            attempt = self.attempts.get(tenant_id)
            if attempt and attempt.client is client:
                attempt.waiting_password = True
                await self.bot.send_message(chat_id, "该账号启用了二步验证，请发送 /password 密码。")
        except Exception as exc:
            logging.warning("QR login failed for tenant=%s: %s", tenant_id, type(exc).__name__)
            if self.attempts.get(tenant_id) and self.attempts[tenant_id].client is client:
                await self.bot.send_message(chat_id, "扫码登录失败，请重新 /login 或使用手机号验证码。")
            await self._discard_attempt(tenant_id, client)

    async def phone(self, event) -> None:
        await self._best_effort_delete(event)
        tenant_id = int(event.sender_id)
        match = re.match(r"^/phone\s+(.+)$", event.raw_text or "")
        if not match:
            await event.respond("格式：/phone +国家码手机号")
            return
        phone = re.sub(r"[\s()-]", "", match.group(1))
        if not re.fullmatch(r"\+\d{7,15}", phone):
            await event.respond("手机号格式无效，请包含国家码，例如 +8613800000000。")
            return
        if not self._allow_login(tenant_id):
            await event.respond("登录尝试过于频繁，请稍后再试。")
            return
        client = await self._new_client()
        try:
            await client.send_code_request(phone)
            await self._replace_attempt(
                tenant_id,
                LoginAttempt(client=client, created_at=time.monotonic(), phone=phone),
            )
            await event.respond("验证码已发送。请发送 /code 12345。验证码消息会立即删除。")
        except FloodWaitError as exc:
            await client.disconnect()
            await event.respond(f"Telegram 要求等待 {exc.seconds} 秒后再试。")
        except Exception as exc:
            await client.disconnect()
            logging.warning("Phone login setup failed tenant=%s: %s", tenant_id, type(exc).__name__)
            await event.respond("验证码发送失败，请检查手机号后重试。")

    async def code(self, event) -> None:
        await self._best_effort_delete(event)
        tenant_id = int(event.sender_id)
        match = re.match(r"^/code\s+(.+)$", event.raw_text or "")
        attempt = self.attempts.get(tenant_id)
        if attempt and time.monotonic() - attempt.created_at > 300:
            await self._discard_attempt(tenant_id, attempt.client)
            attempt = None
        if not match or attempt is None or not attempt.phone:
            await event.respond("请先发送 /phone +国家码手机号。")
            return
        code = re.sub(r"\s", "", match.group(1))
        try:
            await attempt.client.sign_in(phone=attempt.phone, code=code)
            await self._complete_login(tenant_id, attempt.client, event.chat_id)
        except SessionPasswordNeededError:
            attempt.waiting_password = True
            await event.respond("该账号启用了二步验证，请发送 /password 密码。")
        except Exception as exc:
            logging.warning("Code login failed tenant=%s: %s", tenant_id, type(exc).__name__)
            await event.respond("验证码错误或已失效，请重试 /code，必要时重新 /phone。")

    async def password(self, event) -> None:
        await self._best_effort_delete(event)
        tenant_id = int(event.sender_id)
        match = re.match(r"^/password\s+(.+)$", event.raw_text or "", re.S)
        attempt = self.attempts.get(tenant_id)
        if attempt and time.monotonic() - attempt.created_at > 300:
            await self._discard_attempt(tenant_id, attempt.client)
            attempt = None
        if not match or attempt is None or not attempt.waiting_password:
            await event.respond("当前没有等待二步验证密码的登录。")
            return
        try:
            await attempt.client.sign_in(password=match.group(1))
            await self._complete_login(tenant_id, attempt.client, event.chat_id)
        except Exception as exc:
            logging.warning("2FA login failed tenant=%s: %s", tenant_id, type(exc).__name__)
            await event.respond("二步验证密码错误。")

    async def _complete_login(self, tenant_id: int, client: TelegramClient, chat_id: int) -> None:
        me = await client.get_me()
        if me is None or int(me.id) != tenant_id:
            await client.log_out()
            await self._discard_attempt(tenant_id)
            await self.bot.send_message(chat_id, "绑定失败：登录账号必须与当前 Bot 用户相同。")
            return
        session = client.session.save()
        encrypted = self.cipher.encrypt(session)
        display_name = " ".join(part for part in [me.first_name, me.last_name] if part)
        self.repo.save_account(
            tenant_id,
            encrypted,
            username=me.username or "",
            display_name=display_name,
        )
        await client.disconnect()
        self.attempts.pop(tenant_id, None)
        await self.bot.send_message(chat_id, "账号绑定成功。使用 /app 配置转发任务。")

    async def _discard_attempt(
        self,
        tenant_id: int,
        expected_client: Optional[TelegramClient] = None,
    ) -> None:
        attempt = self.attempts.get(tenant_id)
        if attempt and expected_client is not None and attempt.client is not expected_client:
            return
        attempt = self.attempts.pop(tenant_id, None)
        if attempt:
            await attempt.client.disconnect()

    async def app(self, event) -> None:
        url = f"{self.settings.public_base_url}/miniapp"
        await event.respond("打开转发配置：", buttons=[[KeyboardButtonWebView("打开 Mini App", url)]])

    async def web(self, event) -> None:
        tenant_id = int(event.sender_id)
        self.repo.upsert_tenant(tenant_id)
        token = self.repo.create_magic_link(tenant_id)
        url = f"{self.settings.public_base_url}/auth/magic/{token}"
        await event.respond("此链接五分钟内有效且只能使用一次：", buttons=[[Button.url("打开 Web 后台", url)]])

    async def status(self, event) -> None:
        account = self.repo.get_account(int(event.sender_id))
        if not account:
            await event.respond("尚未绑定账号，请使用 /login。")
            return
        tasks = self.repo.list_tasks(int(event.sender_id))
        summary: dict[str, int] = {}
        for task in tasks:
            summary[task.status.value] = summary.get(task.status.value, 0) + 1
        await event.respond(
            f"账号状态：{account['status']}\n任务总数：{len(tasks)}\n"
            + "\n".join(f"{key}: {value}" for key, value in sorted(summary.items()))
        )

    async def _notify_status_changes(self) -> None:
        while True:
            notification = await asyncio.to_thread(
                self.repo.claim_status_notification,
                self.settings.worker_id,
            )
            if notification is None:
                await asyncio.sleep(2)
                continue
            tenant_id = int(notification["tenant_id"])
            event = notification["event"]
            try:
                if event == "account.error":
                    account = await asyncio.to_thread(self.repo.get_account, tenant_id)
                    if account:
                        await self.bot.send_message(
                            tenant_id,
                            f"账号需要处理：{account.get('last_error') or account['status']}\n使用 /status 查看状态。",
                        )
                else:
                    task_id = str(notification.get("metadata", {}).get("task_id", ""))
                    task = await asyncio.to_thread(self.repo.get_task, tenant_id, task_id)
                    if task:
                        if event == "task.completed":
                            text = f"任务“{task.name}”已完成。"
                        else:
                            text = f"任务“{task.name}”发生错误：{task.last_error}"
                        await self.bot.send_message(tenant_id, text)
                await asyncio.to_thread(
                    self.repo.confirm_status_notification,
                    notification["_id"],
                    self.settings.worker_id,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    self.repo.release_status_notification,
                    notification["_id"],
                    self.settings.worker_id,
                    error=type(exc).__name__,
                )
                logging.info(
                    "Status notification skipped tenant=%s error=%s",
                    tenant_id,
                    type(exc).__name__,
                )

    async def logout(self, event) -> None:
        tenant_id = int(event.sender_id)
        account = self.repo.get_account(tenant_id)
        if not account:
            await event.respond("当前没有已绑定账号。")
            return
        self.repo.request_account_delete(tenant_id)
        await event.respond("任务已停止，Telegram 授权正在后台撤销并删除全部数据。")

    @staticmethod
    async def _best_effort_delete(event) -> None:
        try:
            await event.delete()
        except Exception:
            pass


async def async_main() -> None:
    settings = Settings.for_bot()
    repository = MongoRepository(settings.mongo_uri, settings.mongo_db)
    await ControlBot(settings, repository).run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
