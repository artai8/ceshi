"""定时调度模式：每天固定时间自动执行转发任务。"""

import asyncio
import logging
import signal
from datetime import datetime, timedelta

from telethon import TelegramClient

from nb import config
from nb.config import CONFIG, get_SESSION, write_config
from nb.past import forward_with_limit
from nb.plugins import load_async_plugins
from nb.utils import clean_session_files


def _next_run_datetime(run_time: str) -> datetime:
    """计算下一次执行的 datetime（本地时间）。

    如果今天的执行时间尚未过，返回今天的时间；否则返回明天的。
    """
    now = datetime.now()
    parts = run_time.split(":")
    hour, minute = int(parts[0]), int(parts[1])
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


async def _run_daily_tasks(client: TelegramClient) -> None:
    """执行一轮每日转发任务。

    按顺序遍历所有启用的连接，每个连接有独立的 daily_limit。
    当某连接来源消息不足时，剩余配额传递给下一个连接。
    """
    # 重新读取配置以获取最新设置
    cfg = config.read_config()
    config.from_to = await config.load_from_to(client, cfg.forwards)

    carry_over = 0  # 上一个连接未用完的配额

    for i, forward in enumerate(cfg.forwards):
        if not forward.use_this:
            continue

        base_limit = forward.daily_limit
        if base_limit <= 0:
            # daily_limit=0 表示不限制，直接转发全部
            quota = 0
        else:
            quota = base_limit + carry_over

        name = forward.con_name or f"连接 #{i + 1}"
        logging.info(
            f"📋 开始处理 {name}: "
            f"daily_limit={base_limit}, 继承配额={carry_over}, "
            f"实际配额={'不限' if quota == 0 else quota}"
        )

        forwarded, exhausted = await forward_with_limit(
            client, forward, max_count=quota
        )

        if quota > 0 and exhausted and forwarded < quota:
            # 来源耗尽，剩余配额传递给下一个连接
            carry_over = quota - forwarded
            logging.info(
                f"📊 {name} 来源已耗尽: 转发 {forwarded}/{quota} 条, "
                f"剩余 {carry_over} 条配额传递给下一个连接"
            )
        else:
            carry_over = 0
            logging.info(
                f"📊 {name} 完成: 转发 {forwarded} 条, "
                f"来源{'已耗尽' if exhausted else '已满额'}"
            )

    # 持久化 offset 变更
    write_config(cfg)
    logging.info("✅ 本轮定时任务全部完成")


async def schedule_job() -> None:
    """定时调度主入口：每天固定时间执行转发任务，循环等待。"""
    clean_session_files()
    await load_async_plugins()

    if CONFIG.login.user_type != 1:
        logging.warning("⚠️ schedule 模式仅支持用户账号")
        return

    run_time = CONFIG.schedule.run_time
    logging.info(f"🕐 定时调度模式已启动, 每日执行时间: {run_time}")

    SESSION = get_SESSION()
    async with TelegramClient(
        SESSION, CONFIG.login.API_ID, CONFIG.login.API_HASH
    ) as client:
        stop_event = asyncio.Event()

        def _signal_handler():
            logging.info("🛑 收到停止信号，定时调度将在当前循环结束后退出")
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                pass  # Windows 不支持 add_signal_handler

        while not stop_event.is_set():
            # 每轮重新读取 run_time 以支持动态修改
            cfg = config.read_config()
            run_time = cfg.schedule.run_time

            target = _next_run_datetime(run_time)
            now = datetime.now()
            wait_seconds = (target - now).total_seconds()

            logging.info(
                f"⏰ 下次执行: {target.strftime('%Y-%m-%d %H:%M:%S')} "
                f"(等待 {wait_seconds:.0f} 秒)"
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
                break  # stop_event 被设置，退出
            except asyncio.TimeoutError:
                pass  # 超时 = 到达执行时间

            logging.info(f"🚀 定时任务开始执行 ({datetime.now().strftime('%H:%M:%S')})")
            try:
                await _run_daily_tasks(client)
            except Exception as e:
                logging.exception(f"🚨 定时任务执行异常: {e}")

    logging.info("🛑 定时调度模式已退出")
