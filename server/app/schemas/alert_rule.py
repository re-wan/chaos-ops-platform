"""告警规则请求/响应 Schema。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

VALID_SCOPES = {"global", "node", "node_group"}
VALID_CONDITION_TYPES = {"json_dsl", "promql"}
VALID_SEVERITIES = {"critical", "warning", "info"}
VALID_COMPARISON_OPS = {">", "<", ">=", "<=", "==", "!="}


class AlertRuleCreate(BaseModel):
    """创建告警规则请求体。"""

    name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="规则名称，全局唯一",
    )
    description: Optional[str] = Field(default=None, description="规则描述")

    scope: str = Field(description="规则范围：global / node / node_group")
    scope_target: Optional[str] = Field(default=None, description="node_id 或 group_id")

    condition_type: str = Field(description="条件语法类型：json_dsl / promql")
    condition: str = Field(
        ...,
        min_length=1,
        description="JSON DSL 字符串或简化 PromQL 表达式",
    )

    pending_duration_seconds: int = Field(
        default=60,
        ge=0,
        description="pending 持续秒数",
    )
    resolve_duration_seconds: int = Field(
        default=60,
        ge=0,
        description="恢复持续秒数",
    )

    severity: str = Field(description="严重度：critical / warning / info")
    enabled: bool = Field(default=True, description="是否启用")
    notification_channel_ids: list[int] = Field(
        default_factory=list,
        description="通知渠道 ID 列表",
    )

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, v: str) -> str:
        if v not in VALID_SCOPES:
            raise ValueError(f"scope 必须是 {VALID_SCOPES} 之一")
        return v

    @field_validator("condition_type")
    @classmethod
    def validate_condition_type(cls, v: str) -> str:
        if v not in VALID_CONDITION_TYPES:
            raise ValueError(f"condition_type 必须是 {VALID_CONDITION_TYPES} 之一")
        return v

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        if v not in VALID_SEVERITIES:
            raise ValueError(f"severity 必须是 {VALID_SEVERITIES} 之一")
        return v

    @field_validator("scope_target")
    @classmethod
    def validate_scope_target(cls, v: Optional[str], info) -> Optional[str]:
        data = info.data
        scope = data.get("scope")
        if scope in {"node", "node_group"} and not v:
            raise ValueError(f"scope={scope} 时必须提供 scope_target")
        if scope == "global" and v:
            raise ValueError("scope=global 时 scope_target 必须为空")
        return v


class AlertRuleUpdate(BaseModel):
    """更新告警规则请求体。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = None

    scope: Optional[str] = None
    scope_target: Optional[str] = None

    condition_type: Optional[str] = None
    condition: Optional[str] = Field(default=None, min_length=1)

    pending_duration_seconds: Optional[int] = Field(default=None, ge=0)
    resolve_duration_seconds: Optional[int] = Field(default=None, ge=0)

    severity: Optional[str] = None
    enabled: Optional[bool] = None
    notification_channel_ids: Optional[list[int]] = None

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_SCOPES:
            raise ValueError(f"scope 必须是 {VALID_SCOPES} 之一")
        return v

    @field_validator("condition_type")
    @classmethod
    def validate_condition_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_CONDITION_TYPES:
            raise ValueError(f"condition_type 必须是 {VALID_CONDITION_TYPES} 之一")
        return v

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_SEVERITIES:
            raise ValueError(f"severity 必须是 {VALID_SEVERITIES} 之一")
        return v


class AlertRuleRead(BaseModel):
    """告警规则响应模型。"""

    id: int
    name: str
    description: Optional[str]

    scope: str
    scope_target: Optional[str]

    condition_type: str
    condition: str

    pending_duration_seconds: int
    resolve_duration_seconds: int

    severity: str
    enabled: bool
    notification_channel_ids: list[int]

    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
