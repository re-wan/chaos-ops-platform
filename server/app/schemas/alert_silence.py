"""告警静默规则请求/响应 Schema。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class AlertSilenceCreate(BaseModel):
    """创建静默规则请求体。"""

    matchers: dict = Field(description="告警标签匹配条件 JSON 对象")
    starts_at: datetime = Field(description="静默生效开始时间")
    ends_at: datetime = Field(description="静默失效时间")
    comment: Optional[str] = Field(default=None, description="静默说明")

    @field_validator("matchers")
    @classmethod
    def validate_matchers(cls, v: dict) -> dict:
        if not isinstance(v, dict):
            raise ValueError("matchers 必须是 JSON 对象")
        return v

    @field_validator("ends_at")
    @classmethod
    def validate_ends_at(cls, v: datetime, info) -> datetime:
        data = info.data
        starts_at = data.get("starts_at")
        if starts_at is not None and v <= starts_at:
            raise ValueError("ends_at 必须晚于 starts_at")
        return v


class AlertSilenceRead(BaseModel):
    """静默规则响应模型。"""

    id: int
    matchers: dict
    starts_at: datetime
    ends_at: datetime
    comment: Optional[str]
    created_by: Optional[int]
    created_at: datetime

    model_config = {"from_attributes": True}
