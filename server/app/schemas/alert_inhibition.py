"""告警抑制规则请求/响应 Schema。"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class AlertInhibitionCreate(BaseModel):
    """创建抑制规则请求体。"""

    source_matchers: dict = Field(description="父告警匹配条件 JSON 对象")
    target_matchers: dict = Field(description="被抑制告警匹配条件 JSON 对象")
    equal_labels: list[str] = Field(
        default_factory=list,
        description="父告警和子告警必须相同的标签列表",
    )

    @field_validator("source_matchers", "target_matchers")
    @classmethod
    def validate_matchers(cls, v: dict) -> dict:
        if not isinstance(v, dict):
            raise ValueError("matchers 必须是 JSON 对象")
        return v

    @field_validator("equal_labels")
    @classmethod
    def validate_equal_labels(cls, v: list) -> list:
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError("equal_labels 必须是字符串列表")
        return v


class AlertInhibitionRead(BaseModel):
    """抑制规则响应模型。"""

    id: int
    source_matchers: dict
    target_matchers: dict
    equal_labels: list[str]
    created_at: datetime

    model_config = {"from_attributes": True}
