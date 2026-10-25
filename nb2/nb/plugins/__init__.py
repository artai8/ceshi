# nb/plugins/__init__.py

"""Subpackage of nb: plugins."""

import asyncio
import inspect
import logging
import os
from typing import Any, Dict, List, Optional

from telethon.tl.custom.message import Message
from telethon.tl.types import (
    ReplyInlineMarkup,
    KeyboardButtonCallback,
    KeyboardButtonUrl,
    KeyboardButtonSwitchInline,
    KeyboardButtonRow,
)

from nb.plugin_models import ASYNC_PLUGIN_IDS, InlineButtonMode
from nb.utils import cleanup, stamp, describe_message, describe_nb_message

PLUGIN_ORDER = [
    "filter", "ocr", "replace", "caption", "fmt", "mark", "spoiler"
]


def _legacy_config():
    from nb import config as config_module

    return config_module.CONFIG


# =====================================================================
#  Inline Button 处理
# =====================================================================


def _replace_in_string(original: str, replacements: Dict[str, str]) -> str:
    """对字符串依次应用所有替换规则（纯字符串替换）"""
    result = original
    for old, new in replacements.items():
        result = result.replace(old, new)
    return result


def _process_reply_markup(
    reply_markup,
    mode: InlineButtonMode,
    url_replacements: Dict[str, str],
    text_replacements: Dict[str, str],
):
    """
    根据配置处理 reply_markup。

    Returns:
        处理后的 markup，或 None（表示移除）
    """
    # 没有 markup 则无需处理
    if reply_markup is None:
        return None

    # 不是 inline markup（比如 ReplyKeyboardMarkup），直接移除
    if not isinstance(reply_markup, ReplyInlineMarkup):
        return None

    # 模式: 完全移除
    if mode == InlineButtonMode.REMOVE:
        return None

    # 模式: 只替换 URL / 替换 URL + 文字
    new_rows = []
    for row in reply_markup.rows:
        new_buttons = []
        for button in row.buttons:
            new_button = _process_single_button(
                button, mode, url_replacements, text_replacements
            )
            if new_button is not None:
                new_buttons.append(new_button)
        if new_buttons:
            new_rows.append(KeyboardButtonRow(buttons=new_buttons))

    if not new_rows:
        return None
    return ReplyInlineMarkup(rows=new_rows)


def _process_single_button(button, mode, url_replacements, text_replacements):
    """处理单个按钮，返回新按钮或 None"""

    btn_text = button.text or ""

    # 是否替换文字（仅 REPLACE_ALL 模式）
    if mode == InlineButtonMode.REPLACE_ALL and text_replacements:
        btn_text = _replace_in_string(btn_text, text_replacements)

    # URL 按钮
    if isinstance(button, KeyboardButtonUrl):
        url = button.url or ""
        if url_replacements:
            url = _replace_in_string(url, url_replacements)
        return KeyboardButtonUrl(text=btn_text, url=url)

    # Callback 按钮 — 原样保留结构，只改文字
    if isinstance(button, KeyboardButtonCallback):
        if mode == InlineButtonMode.REPLACE_ALL:
            return KeyboardButtonCallback(
                text=btn_text,
                data=button.data,
                requires_password=getattr(button, 'requires_password', False),
            )
        # REPLACE_URL 模式不需要改 callback 按钮
        return button

    # SwitchInline 按钮
    if isinstance(button, KeyboardButtonSwitchInline):
        if mode == InlineButtonMode.REPLACE_ALL:
            return KeyboardButtonSwitchInline(
                text=btn_text,
                query=button.query,
                same_peer=getattr(button, 'same_peer', False),
            )
        return button

    # 其他类型按钮原样保留
    return button


# =====================================================================
#  NbMessage
# =====================================================================


class NbMessage:
    def __init__(self, message: Message, inline_config=None, download_dir: Optional[str] = None) -> None:
        self.message = message
        self.text = self.message.text or ""
        self.raw_text = self.message.raw_text or ""
        self.sender_id = self.message.sender_id
        self.file_type = self.guess_file_type()
        self.new_file = None
        self.cleanup = False
        self.reply_to = None
        self.client = self.message.client
        self.inline_config = inline_config
        self.download_dir = download_dir

        # Inline Button 处理
        self.reply_markup = self._build_reply_markup()

    def _build_reply_markup(self):
        """根据配置处理消息的 reply_markup"""
        original_markup = self.message.reply_markup
        if original_markup is None:
            return None

        inline_cfg = self.inline_config or _legacy_config().plugins.inline
        if not inline_cfg.check:
            # 插件未启用 → 默认移除，避免转发报错
            return None

        return _process_reply_markup(
            original_markup,
            inline_cfg.mode,
            inline_cfg.url_replacements,
            inline_cfg.text_replacements,
        )

    async def get_file(self) -> str:
        if self.file_type == "nofile":
            raise FileNotFoundError("No file exists in this message.")
        downloaded = await self.message.download_media(self.download_dir or "")
        self.file = downloaded if self.download_dir else stamp(downloaded, self.sender_id)
        return self.file

    def guess_file_type(self) -> str:
        for ft in ["photo", "video", "gif", "audio", "document", "sticker", "contact"]:
            if getattr(self.message, ft, None):
                return ft
        return "nofile"

    def clear(self) -> None:
        if self.new_file and self.cleanup:
            cleanup(self.new_file)
            self.new_file = None


# =====================================================================
#  NbPlugin 基类 & 加载逻辑
# =====================================================================


class NbPlugin:
    id_ = "plugin"

    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data

    async def __ainit__(self) -> None:
        pass

    def modify(self, tm: NbMessage) -> NbMessage:
        return tm


class PluginPipelineError(RuntimeError):
    """An enabled task plugin could not be applied safely."""


class PluginPipeline:
    """A task-scoped plugin pipeline used by the multi-tenant runtime."""

    def __init__(self, plugins_config, *, download_dir: Optional[str] = None) -> None:
        self.config = plugins_config
        self.download_dir = download_dir
        self.plugins: Dict[str, NbPlugin] = {}
        for pid in PLUGIN_ORDER:
            cfg = getattr(plugins_config, pid, None)
            if not cfg or not getattr(cfg, "check", False):
                continue
            try:
                mod = __import__(f"nb.plugins.{pid}", fromlist=[""])
                cls = getattr(mod, f"Nb{pid.title()}")
                plugin = cls(cfg)
                if plugin.id_ != pid:
                    raise ValueError(f"Plugin ID mismatch: {plugin.id_} != {pid}")
                self.plugins[pid] = plugin
            except Exception as exc:
                raise PluginPipelineError(f"Failed to build enabled plugin {pid}") from exc

    async def initialize(self) -> None:
        for pid, plugin in self.plugins.items():
            try:
                await plugin.__ainit__()
            except Exception as exc:
                raise PluginPipelineError(f"Failed to initialize enabled plugin {pid}") from exc

    async def apply(self, message: Message) -> Optional[NbMessage]:
        tm = NbMessage(message, inline_config=self.config.inline, download_dir=self.download_dir)
        for pid in PLUGIN_ORDER:
            plugin = self.plugins.get(pid)
            if plugin is None:
                continue
            try:
                result = await plugin.modify(tm) if inspect.iscoroutinefunction(plugin.modify) else plugin.modify(tm)
                if not result:
                    tm.clear()
                    return None
                tm = result
            except Exception as exc:
                tm.clear()
                raise PluginPipelineError(f"Task plugin failed: {pid}") from exc
        return tm

    async def apply_group(
        self,
        messages: List[Message],
        *,
        skip_plugins: Optional[List[str]] = None,
        fail_open: bool = False,
    ) -> List[NbMessage]:
        tms = [
            NbMessage(message, inline_config=self.config.inline, download_dir=self.download_dir)
            for message in messages
        ]
        skipped = set(skip_plugins or [])
        for pid in PLUGIN_ORDER:
            if pid in skipped:
                continue
            plugin = self.plugins.get(pid)
            if plugin is None:
                continue
            try:
                if hasattr(plugin, "modify_group"):
                    method = plugin.modify_group
                    tms = await method(tms) if inspect.iscoroutinefunction(method) else method(tms)
                else:
                    values = []
                    for tm in tms:
                        result = await plugin.modify(tm) if inspect.iscoroutinefunction(plugin.modify) else plugin.modify(tm)
                        if result:
                            values.append(result)
                    tms = values
            except Exception as exc:
                for tm in tms or []:
                    tm.clear()
                raise PluginPipelineError(f"Task group plugin failed: {pid}") from exc
            if tms is None:
                tms = []
            tms = [tm for tm in tms if tm]
            if fail_open and pid == "filter" and not tms:
                tms = [
                    NbMessage(
                        message,
                        inline_config=self.config.inline,
                        download_dir=self.download_dir,
                    )
                    for message in messages
                ]
        return tms

_plugins: Dict[str, NbPlugin] = {}


def load_plugins() -> Dict[str, NbPlugin]:
    global _plugins
    _plugins = {}
    plugins_config = _legacy_config().plugins

    for pid in PLUGIN_ORDER:
        cfg = getattr(plugins_config, pid, None)
        if not cfg or not getattr(cfg, "check", False):
            continue

        try:
            mod = __import__(f"nb.plugins.{pid}", fromlist=[""])
            cls = getattr(mod, f"Nb{pid.title()}")
            plugin = cls(cfg)
            if plugin.id_ != pid:
                logging.error(f"ID mismatch: {plugin.id_} != {pid}")
                continue
            _plugins[pid] = plugin
            logging.info(f"✅ 插件加载: {pid}")
        except Exception as e:
            logging.error(f"❌ 加载失败 {pid}: {e}")

    return _plugins


def reload_plugins(reload_async: bool = True) -> Dict[str, NbPlugin]:
    plugins = load_plugins()
    if not reload_async:
        return plugins

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        loop.create_task(load_async_plugins())
        logging.info("🔄 插件已重载，异步插件初始化已加入事件循环")
    else:
        asyncio.run(load_async_plugins())
        logging.info("🔄 插件已重载")
    return plugins


async def apply_plugins(message: Message) -> Optional[NbMessage]:
    tm = NbMessage(message)
    logging.info(f"🧩 插件链开始 source={describe_message(message)} initial={describe_nb_message(tm)}")
    for pid in PLUGIN_ORDER:
        if pid not in _plugins:
            continue
        plugin = _plugins[pid]
        try:
            before = describe_nb_message(tm)
            logging.info(f"🧩 插件执行 [{pid}] before={before}")
            if inspect.iscoroutinefunction(plugin.modify):
                ntm = await plugin.modify(tm)
            else:
                ntm = plugin.modify(tm)
            if not ntm:
                logging.info(f"🧩 插件过滤 [{pid}] source={describe_message(message)}")
                tm.clear()
                return None
            tm = ntm
            logging.info(f"🧩 插件执行 [{pid}] after={describe_nb_message(tm)}")
        except Exception as e:
            logging.error(f"❌ 插件执行失败 [{pid}]: {e}")
    logging.info(f"🧩 插件链完成 source={describe_message(message)} final={describe_nb_message(tm)}")
    return tm


async def apply_plugins_to_group(
    messages: List[Message],
    skip_plugins: Optional[List[str]] = None,
    fail_open: bool = False,
    base_text: Optional[str] = None,
) -> List[NbMessage]:
    tms = [NbMessage(msg) for msg in messages]
    logging.info(
        f"🧩 组插件链开始 count={len(messages)} messages={[describe_message(msg) for msg in messages]}"
    )
    if base_text and tms:
        tms[0].text = base_text
        tms[0].raw_text = base_text
    skip = set(skip_plugins or [])
    for pid in PLUGIN_ORDER:
        if pid in skip:
            logging.info(f"🧩 组插件跳过 [{pid}] reason=skip_plugins")
            continue
        if pid not in _plugins:
            continue
        plugin = _plugins[pid]
        try:
            logging.info(f"🧩 组插件执行 [{pid}] before_count={len(tms)}")
            if hasattr(plugin, 'modify_group'):
                if inspect.iscoroutinefunction(plugin.modify_group):
                    tms = await plugin.modify_group(tms)
                else:
                    tms = plugin.modify_group(tms)
            else:
                new_tms = []
                for tm in tms:
                    if inspect.iscoroutinefunction(plugin.modify):
                        result = await plugin.modify(tm)
                    else:
                        result = plugin.modify(tm)
                    if result:
                        new_tms.append(result)
                tms = new_tms
            logging.info(
                f"🧩 组插件执行 [{pid}] after_count={len(tms)} summaries={[describe_nb_message(tm) for tm in tms]}"
            )
        except Exception as e:
            logging.error(f"❌ 组插件失败 [{pid}]: {e}")
        tms = [tm for tm in tms if tm]
    if fail_open and not tms:
        tms = [NbMessage(msg) for msg in messages]
        if base_text and tms:
            tms[0].text = base_text
            tms[0].raw_text = base_text
        logging.info("🧩 组插件 fail_open 生效，恢复原始消息模板")
    logging.info(f"🧩 组插件链完成 count={len(tms)} summaries={[describe_nb_message(tm) for tm in tms]}")
    return tms


async def load_async_plugins() -> None:
    for pid in ASYNC_PLUGIN_IDS:
        if pid in _plugins:
            try:
                await _plugins[pid].__ainit__()
                logging.info(f"🔌 异步插件已加载: {pid}")
            except Exception as e:
                logging.error(f"❌ 异步插件初始化失败 [{pid}]: {e}")


_plugins = {} if os.getenv("NB_MULTITENANT") == "1" else load_plugins()
