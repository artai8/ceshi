"""Public models for tenant accounts and forwarding tasks."""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nb.plugin_models import PluginConfig


class TaskMode(str, Enum):
    LIVE = "live"
    PAST = "past"
    SCHEDULE = "schedule"


class TaskStatus(str, Enum):
    DRAFT = "draft"
    VALIDATING = "validating"
    ACTIVE = "active"
    PAUSED = "paused"
    RUNNING = "running"
    ERROR = "error"
    COMPLETED = "completed"


class AccountStatus(str, Enum):
    ACTIVE = "active"
    REAUTH_REQUIRED = "reauth_required"
    DELETING = "deleting"
    DISABLED = "disabled"


class CommentsSettings(BaseModel):
    enabled: bool = False
    source_mode: Literal["comments", "discussion"] = "comments"
    source_discussion_group: Optional[str | int] = None
    dest_mode: Literal["comments", "discussion"] = "comments"
    dest_discussion_groups: List[str | int] = Field(default_factory=list)
    only_media: bool = False
    include_text_comments: bool = True
    skip_bot_comments: bool = False
    skip_admin_comments: bool = False
    post_mapping_mode: Literal["auto", "manual"] = "auto"
    manual_post_mapping: Dict[str, str] = Field(default_factory=dict)
    merge_comment_media: bool = False
    merge_wait_seconds: int = Field(default=60, ge=10, le=600)


class BotMediaSettings(BaseModel):
    enabled: bool = True
    auto_comment_trigger_enabled: bool = True
    enable_keyword_trigger: bool = True
    enable_pagination: bool = True
    pagination_mode: Literal["auto", "any"] = "auto"
    pagination_keywords: List[str] = Field(default_factory=list)
    pagination_ignore_keywords: List[str] = Field(default_factory=list)
    tme_link_blacklist: List[str] = Field(default_factory=list)
    ignore_filter: bool = True
    force_forward_on_empty: bool = True
    poll_interval: float = Field(default=1.2, ge=0.1, le=30)
    wait_timeout: float = Field(default=12, ge=1, le=120)
    max_pages: int = Field(default=5, ge=1, le=50)
    recent_limit: int = Field(default=80, ge=10, le=1000)
    comment_keyword_prefixes: List[str] = Field(
        default_factory=lambda: ["评论区回复", "评论区发送", "在评论区回复", "在评论区发送"]
    )
    comment_keyword_suffixes: List[str] = Field(
        default_factory=lambda: ["获取资源", "领取", "获取", "得到内容"]
    )
    comment_keyword_from_comments_enabled: bool = True


class LiveTaskSettings(BaseModel):
    delete_sync: bool = False
    delete_on_edit: Optional[str] = ".deleteMe"


class PastTaskSettings(BaseModel):
    delay: int = Field(default=0, ge=0, le=100)


class ScheduleTaskSettings(BaseModel):
    run_time: str = "08:00"
    daily_limit: int = Field(default=0, ge=0)

    @field_validator("run_time")
    @classmethod
    def validate_run_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value):
            raise ValueError("run_time must be HH:MM")
        hour, minute = (int(part) for part in value.split(":"))
        return f"{hour:02d}:{minute:02d}"


class ResolvedEndpoint(BaseModel):
    raw: str
    peer_id: int
    title: str = ""
    username: str = ""


class ForwardTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="新转发任务", min_length=1, max_length=100)
    mode: TaskMode = TaskMode.LIVE
    sources: List[str] = Field(default_factory=list)
    destinations: List[str] = Field(default_factory=list)
    show_forwarded_from: bool = False
    comments: CommentsSettings = Field(default_factory=CommentsSettings)
    bot_media: BotMediaSettings = Field(default_factory=BotMediaSettings)
    live: LiveTaskSettings = Field(default_factory=LiveTaskSettings)
    past: PastTaskSettings = Field(default_factory=PastTaskSettings)
    schedule: ScheduleTaskSettings = Field(default_factory=ScheduleTaskSettings)
    plugins: PluginConfig = Field(default_factory=PluginConfig)
    watermark_asset_id: Optional[str] = None

    @field_validator("sources", "destinations")
    @classmethod
    def clean_endpoints(cls, values: List[str]) -> List[str]:
        result: List[str] = []
        seen = set()
        for raw in values:
            value = str(raw).strip()
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result


class ForwardTask(ForwardTaskInput):
    id: str
    tenant_id: int
    status: TaskStatus = TaskStatus.DRAFT
    revision: int = 1
    runtime_epoch: int = Field(default=0, exclude=True)
    pending_action: Optional[str] = None
    resolved_sources: List[ResolvedEndpoint] = Field(default_factory=list)
    resolved_destinations: List[ResolvedEndpoint] = Field(default_factory=list)
    offsets: Dict[str, int] = Field(default_factory=dict)
    last_error: str = ""
    created_at: datetime
    updated_at: datetime


class TaskUpdate(BaseModel):
    revision: int = Field(ge=1)
    task: ForwardTaskInput


class TenantView(BaseModel):
    tenant_id: int
    is_admin: bool = False
    account_status: Optional[AccountStatus] = None


class AccountView(BaseModel):
    tenant_id: int
    status: AccountStatus
    username: str = ""
    display_name: str = ""
    last_error: str = ""
    updated_at: datetime


class MiniAppAuthRequest(BaseModel):
    init_data: str


class SessionResponse(BaseModel):
    token: str
    expires_at: datetime
