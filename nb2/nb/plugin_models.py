from enum import Enum
from typing import Any, Dict, List

# ✅ Pydantic v2 导入 Field
from pydantic import BaseModel, ConfigDict, Field
from watermark import Position


class FileType(str, Enum):
    AUDIO = "audio"
    GIF = "gif"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"
    STICKER = "sticker"
    CONTACT = "contact"
    PHOTO = "photo"
    DOCUMENT = "document"
    NOFILE = "nofile"


class FilterList(BaseModel):
    # ✅ v2 写法: default_factory=list
    blacklist: List[str] = Field(default_factory=list)
    whitelist: List[str] = Field(default_factory=list)


class FilesFilterList(BaseModel):
    blacklist: List[FileType] = Field(default_factory=list)
    whitelist: List[FileType] = Field(default_factory=list)


class TextFilter(FilterList):
    case_sensitive: bool = False
    regex: bool = False


class Style(str, Enum):
    BOLD = "bold"
    ITALICS = "italics"
    CODE = "code"
    STRIKE = "strike"
    PLAIN = "plain"
    PRESERVE = "preserve"


STYLE_CODES = {"bold": "**", "italics": "__", "code": "`", "strike": "~~", "plain": ""}


# define plugin configs


class Filters(BaseModel):
    check: bool = False
    users: FilterList = Field(default_factory=FilterList)
    files: FilesFilterList = Field(default_factory=FilesFilterList)
    text: TextFilter = Field(default_factory=TextFilter)


class Format(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    check: bool = False
    style: Style = Style.PRESERVE


class MarkConfig(BaseModel):
    check: bool = False
    image: str = "image.png"
    position: Position = Position.centre
    frame_rate: int = 15


class OcrConfig(BaseModel):
    check: bool = False
    lang: str = "chi_sim"


class Replace(BaseModel):
    check: bool = False
    # ✅ v2 写法: default_factory=dict
    text: Dict[str, str] = Field(default_factory=dict)
    text_raw: str = ""
    regex: bool = False


class Caption(BaseModel):
    check: bool = False
    header: str = ""
    footer: str = ""


class SpoilerConfig(BaseModel):
    check: bool = False


# ===================== Inline Button 配置 =====================


class InlineButtonMode(str, Enum):
    """Inline Button 处理模式"""
    REMOVE = "remove"             # 完全移除所有按钮
    REPLACE_URL = "replace_url"   # 保留按钮结构，只替换 URL
    REPLACE_ALL = "replace_all"   # 替换按钮文字 + 替换 URL


class InlineButtonConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    check: bool = False
    mode: InlineButtonMode = InlineButtonMode.REMOVE
    url_replacements: Dict[str, str] = Field(default_factory=dict)       # 旧URL片段 -> 新URL片段
    url_replacements_raw: str = ""               # Web UI 用的原始文本
    text_replacements: Dict[str, str] = Field(default_factory=dict)       # 旧按钮文字 -> 新按钮文字
    text_replacements_raw: str = ""              # Web UI 用的原始文本


class PluginConfig(BaseModel):
    filter: Filters = Field(default_factory=Filters)
    fmt: Format = Field(default_factory=Format)
    mark: MarkConfig = Field(default_factory=MarkConfig)
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    replace: Replace = Field(default_factory=Replace)
    caption: Caption = Field(default_factory=Caption)
    spoiler: SpoilerConfig = Field(default_factory=SpoilerConfig)
    inline: InlineButtonConfig = Field(default_factory=InlineButtonConfig)


# List of plugins that need to load asynchronously
ASYNC_PLUGIN_IDS = []
