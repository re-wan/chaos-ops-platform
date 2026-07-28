"""自愈规则请求/响应 Schema。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class HealRuleCreate(BaseModel):
    """创建自愈规则请求体。"""

    name: str = Field(..., min_length=1, max_length=128, description="规则名称")
    description: Optional[str] = Field(default=None, description="规则说明")
    alert_rule_id: int = Field(description="关联的告警规则 ID")
    action_id: str = Field(..., min_length=1, max_length=128, description="动作标识")
    action_params: dict = Field(
        default_factory=dict,
        description="动作参数，支持模板变量",
    )
    enabled: bool = Field(default=True, description="是否启用")
    auto_execute: bool = Field(default=False, description="是否自动执行")
    priority: int = Field(default=0, description="优先级")
    verification_config: Optional[dict] = Field(
        default=None, description="验证配置 JSON"
    )

    @field_validator("verification_config")
    @classmethod
    def validate_verification_config(cls, v: Optional[dict]) -> Optional[dict]:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("verification_config 必须是 JSON 对象")
        return v

    @field_validator("action_params")
    @classmethod
    def validate_action_params(cls, v: dict) -> dict:
        if not isinstance(v, dict):
            raise ValueError("action_params 必须是 JSON 对象")
        return v


class HealRuleUpdate(BaseModel):
    """更新自愈规则请求体。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = None
    action_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    action_params: Optional[dict] = None
    enabled: Optional[bool] = None
    auto_execute: Optional[bool] = None
    priority: Optional[int] = None
    verification_config: Optional[dict] = None

    @field_validator("verification_config")
    @classmethod
    def validate_verification_config_update(
        cls, v: Optional[dict]
    ) -> Optional[dict]:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("verification_config 必须是 JSON 对象")
        return v

    @field_validator("action_params")
    @classmethod
    def validate_action_params(cls, v: Optional[dict]) -> Optional[dict]:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("action_params 必须是 JSON 对象")
        return v


class HealRuleRead(BaseModel):
    """自愈规则响应模型。"""

    id: int
    name: str
    description: Optional[str]
    alert_rule_id: int
    action_id: str
    action_params: dict
    enabled: bool
    auto_execute: bool
    priority: int
    verification_config: Optional[dict]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
