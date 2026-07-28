"""通知渠道请求/响应 Schema。"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

VALID_CHANNEL_TYPES = {"email", "webhook", "dingtalk", "wecom", "lark", "slack"}
VALID_CHANNEL_LANGS = {"zh", "en"}


class NotificationChannelCreate(BaseModel):
    """创建通知渠道请求体。"""

    name: str = Field(..., min_length=1, max_length=128, description="渠道名称")
    channel_type: str = Field(description="渠道类型：email / webhook / dingtalk / wecom / lark / slack")
    enabled: bool = Field(default=True, description="是否启用")
    lang: str = Field(default="zh", description="通知语言：zh / en")
    config: dict[str, Any] = Field(description="渠道配置")

    @field_validator("channel_type")
    @classmethod
    def validate_channel_type(cls, v: str) -> str:
        if v not in VALID_CHANNEL_TYPES:
            raise ValueError(f"channel_type 必须是 {VALID_CHANNEL_TYPES} 之一")
        return v

    @field_validator("lang")
    @classmethod
    def validate_lang(cls, v: str) -> str:
        if v not in VALID_CHANNEL_LANGS:
            raise ValueError(f"lang 必须是 {VALID_CHANNEL_LANGS} 之一")
        return v


class NotificationChannelUpdate(BaseModel):
    """更新通知渠道请求体。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    channel_type: Optional[str] = None
    enabled: Optional[bool] = None
    lang: Optional[str] = None
    config: Optional[dict[str, Any]] = None

    @field_validator("channel_type")
    @classmethod
    def validate_channel_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_CHANNEL_TYPES:
            raise ValueError(f"channel_type 必须是 {VALID_CHANNEL_TYPES} 之一")
        return v

    @field_validator("lang")
    @classmethod
    def validate_lang(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_CHANNEL_LANGS:
            raise ValueError(f"lang 必须是 {VALID_CHANNEL_LANGS} 之一")
        return v


class NotificationChannelRead(BaseModel):
    """通知渠道响应模型。

    config 返回脱敏后的配置，敏感字段显示为 ***。
    """

    id: int
    name: str
    channel_type: str
    enabled: bool
    lang: str
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NotificationChannelTestPayload(BaseModel):
    """测试通知请求体。"""

    payload: Optional[dict[str, Any]] = Field(
        default=None, description="自定义测试 payload"
    )
