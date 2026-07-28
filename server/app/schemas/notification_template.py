"""通知模板请求/响应 Schema。"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

VALID_FORMATS = {"text", "markdown", "html"}
VALID_EVENT_TYPES = {
    "alert.firing",
    "alert.resolved",
    "incident.created",
    "incident.resolved",
}
VALID_CHANNEL_TYPES = {"email", "webhook"}

_MAX_BODY_TEMPLATE_BYTES = 64 * 1024


class NotificationTemplateCreate(BaseModel):
    """创建自定义模板请求体。"""

    name: str = Field(..., min_length=1, max_length=128, description="模板名称")
    event_type: str = Field(description="事件类型")
    channel_type: str = Field(description="渠道类型")
    subject_template: Optional[str] = Field(
        default=None, description="邮件主题模板"
    )
    body_template: str = Field(..., description="正文模板")
    format: str = Field(default="markdown", description="输出格式")

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        if v not in VALID_EVENT_TYPES:
            raise ValueError(f"event_type 必须是 {VALID_EVENT_TYPES} 之一")
        return v

    @field_validator("channel_type")
    @classmethod
    def validate_channel_type(cls, v: str) -> str:
        if v not in VALID_CHANNEL_TYPES:
            raise ValueError(f"channel_type 必须是 {VALID_CHANNEL_TYPES} 之一")
        return v

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in VALID_FORMATS:
            raise ValueError(f"format 必须是 {VALID_FORMATS} 之一")
        return v

    @field_validator("body_template")
    @classmethod
    def validate_body_size(cls, v: str) -> str:
        if v is not None and len(v.encode("utf-8")) > _MAX_BODY_TEMPLATE_BYTES:
            raise ValueError("body_template 大小不能超过 64KB")
        return v


class NotificationTemplateUpdate(BaseModel):
    """更新自定义模板请求体。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    subject_template: Optional[str] = None
    body_template: Optional[str] = None
    format: Optional[str] = None

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_FORMATS:
            raise ValueError(f"format 必须是 {VALID_FORMATS} 之一")
        return v

    @field_validator("body_template")
    @classmethod
    def validate_body_size(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and len(v.encode("utf-8")) > _MAX_BODY_TEMPLATE_BYTES:
            raise ValueError("body_template 大小不能超过 64KB")
        return v


class NotificationTemplateRead(BaseModel):
    """通知模板响应模型。"""

    id: int
    name: str
    event_type: str
    channel_type: str
    subject_template: Optional[str]
    body_template: str
    format: str
    is_default: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NotificationTemplatePreviewRequest(BaseModel):
    """模板预览请求体。"""

    context: dict[str, Any] = Field(
        default_factory=dict, description="渲染上下文"
    )


class NotificationTemplatePreviewResponse(BaseModel):
    """模板预览响应。"""

    subject: Optional[str]
    body: str
    format: str
