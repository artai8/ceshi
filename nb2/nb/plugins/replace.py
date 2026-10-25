# nb/plugins/replace.py —— 已修复：no name 'replace' is not defined

import logging
from typing import Any, Dict, List

# ✅ 关键修复：显式导入 replace 函数
from nb.utils import replace as utils_replace
from nb.plugins import NbMessage, NbPlugin


class NbReplace(NbPlugin):
    id_ = "replace"

    def __init__(self, data):
        self.replace = data
        logging.info(f"🔧 加载替换规则: {data.text}")

    def modify(self, tm: NbMessage) -> NbMessage:
        raw_text = tm.raw_text  # ✅ 始终基于原始文本操作
        if not raw_text:
            return tm

        current_text = raw_text
        for original, new in self.replace.text.items():
            current_text = utils_replace(original, new, current_text, self.replace.regex)

        tm.text = current_text
        return tm

    def modify_group(self, tms: List[NbMessage]) -> List[NbMessage]:
        for tm in tms:
            if tm.raw_text:
                text = tm.raw_text
                for original, new in self.replace.text.items():
                    text = utils_replace(original, new, text, self.replace.regex)
                tm.text = text
        return tms
