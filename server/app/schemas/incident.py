"""事件（Incident）请求/响应 Schema。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.incident_alert import IncidentAlertRead

VALID_STATUSES = {"open", "acknowledged", "resolved", "closed"}
VALID_SEVERITIES = {"critical", "warning", "info"}


class IncidentCreate(BaseModel):
    """手动创建事件请求体。"""

    title: str = Field(..., min_length=1, max_length=256, description="事件标题")
    description: Optional[str] = Field(default=None, description="事件描述")
    severity: str = Field(description="严重度：critical / warning / info")
    assigned_to: Optional[int] = Field(default=None, description="负责人用户 ID")
    alert_event_ids: list[int] = Field(
        default_factory=list,
        description="关联的告警事件 ID 列表",
    )

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        if v not in VALID_SEVERITIES:
            raise ValueError(f"severity 必须是 {VALID_SEVERITIES} 之一")
        return v


class IncidentUpdate(BaseModel):
    """更新事件请求体（仅允许修改基础信息，状态必须通过动作接口流转）。"""

    title: Optional[str] = Field(default=None, min_length=1, max_length=256)
    description: Optional[str] = None
    assigned_to: Optional[int] = None


class IncidentRead(BaseModel):
    """事件响应模型。"""

    id: int
    title: str
    description: Optional[str]
    severity: str
    status: str
    source: str
    created_by: Optional[int]
    assigned_to: Optional[int]
    rule_id: Optional[int]
    node_id: Optional[str]
    started_at: datetime
    acknowledged_at: Optional[datetime]
    resolved_at: Optional[datetime]
    closed_at: Optional[datetime]
    is_deleted: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IncidentDetailRead(IncidentRead):
    """事件详情响应模型，包含关联告警事件列表。"""

    alerts: list[IncidentAlertRead] = Field(default_factory=list)


class IncidentMergeRequest(BaseModel):
    """合并事件请求体。"""

    target_incident_id: int = Field(description="目标事件 ID")
