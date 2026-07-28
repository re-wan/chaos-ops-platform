"""事件与告警关联（IncidentAlert）数据模型。"""

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class IncidentAlert(SQLModel, table=True):
    """事件与告警事件关联表。

    一个 Incident 可聚合多个 AlertEvent，通过 (incident_id, alert_event_id)
    唯一约束避免重复关联。
    """

    __tablename__ = "incident_alerts"
    __table_args__ = (
        UniqueConstraint(
            "incident_id",
            "alert_event_id",
            name="uix_incident_alert_event",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    incident_id: int = Field(index=True, description="事件 ID")
    alert_event_id: int = Field(index=True, description="告警事件 ID")
    added_at: datetime = Field(default_factory=now_utc, description="关联时间")
