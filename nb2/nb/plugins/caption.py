# nb/plugins/caption.py

import logging
from typing import List

from nb.plugins import NbMessage, NbPlugin


class NbCaption(NbPlugin):
    id_ = "caption"

    def __init__(self, data) -> None:
        self.caption = data
        self._header = data.header.strip() if data.header else ""
        self._footer = data.footer.strip() if data.footer else ""
        logging.info(f"📝 加载标题插件: header='{self._header}', footer='{self._footer}'")

    def modify(self, tm: NbMessage) -> NbMessage:
        """单条消息：正常添加 header 和 footer"""
        original_text = tm.text or ""
        has_content = bool(original_text.strip())
        final_text = original_text

        if self._header and not final_text.startswith(self._header):
            sep = "\n\n" if has_content else ""
            final_text = self._header + sep + final_text

        if self._footer and not final_text.endswith(self._footer):
            sep = "\n\n" if has_content else ""
            final_text += sep + self._footer

        tm.text = final_text
        return tm

    def modify_group(self, tms: List[NbMessage]) -> List[NbMessage]:
        """媒体组：整组只添加一次 header/footer。

        策略：
        - 找到组内第一条有文字的消息，添加 header
        - 找到组内最后一条有文字的消息，添加 footer
        - 如果没有任何文字，则在第一条消息上添加 header+footer
        - 如果 header 和 footer 落在同一条消息上，合并处理
        """
        if not tms:
            return tms

        # 找到有文字内容的消息索引
        text_indices = [
            i for i, tm in enumerate(tms)
            if tm.text and tm.text.strip()
        ]

        if not text_indices:
            # 所有消息都没有文字 → 在第一条上添加
            if self._header or self._footer:
                combined = ""
                if self._header:
                    combined = self._header
                if self._footer:
                    if combined:
                        combined += "\n\n" + self._footer
                    else:
                        combined = self._footer
                tms[0].text = combined
            return tms

        first_text_idx = text_indices[0]
        last_text_idx = text_indices[-1]

        # 只在第一条有文字的消息前加 header
        if self._header:
            tm = tms[first_text_idx]
            original = tm.text or ""
            if not original.startswith(self._header):
                sep = "\n\n" if original.strip() else ""
                tm.text = self._header + sep + original

        # 只在最后一条有文字的消息后加 footer
        if self._footer:
            tm = tms[last_text_idx]
            original = tm.text or ""
            if not original.endswith(self._footer):
                sep = "\n\n" if original.strip() else ""
                tm.text = original + sep + self._footer

        return tms
