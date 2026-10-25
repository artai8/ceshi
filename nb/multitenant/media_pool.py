"""Bounded process pool for CPU-heavy tenant media plugins."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ProcessPoolExecutor
from functools import partial


_EXECUTOR = ProcessPoolExecutor(max_workers=max(1, min(2, os.cpu_count() or 1)))


async def run_cpu(function, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, partial(function, *args))
