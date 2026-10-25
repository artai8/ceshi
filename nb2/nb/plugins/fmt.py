# nb/plugins/fmt.py —— 已修复版本

import logging
from enum import Enum
from typing import Any, Dict

from nb.plugin_models import STYLE_CODES, Format, Style
from nb.plugins import NbMessage, NbPlugin


class NbFmt(NbPlugin):
    id_ = "fmt"

    def __init__(self, data) -> None:
        self.format = data
        logging.info(f"🎨 加载格式插件: {data.style}")

    def modify(self, tm: NbMessage) -> NbMessage:
        if self.format.style is Style.PRESERVE or not tm.raw_text:
            return tm

        style_str = self.format.style.value
        code = STYLE_CODES.get(style_str)
        if not code:
            return tm

        tm.text = f"{code}{tm.raw_text}{code}"
        return tm
