"""事件时间线（IncidentEvent）数据模型。"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class IncidentEvent(SQLModel, table=True):
    """事件时间线表。

    记录 Incident 生命周期中的告警、自愈、用户操作、通知等事件。
    时间线事件一旦写入原则上不修改，仅支持追加注释。
    """

    __tablename__ = "incident_events"
    __table_args__ = (
        Index("ix_incident_events_incident_timestamp", "incident_id", "timestamp"),
        # Phase 3 Step 05：全局时间线按 timestamp 范围扫描。
        Index("ix_incident_events_timestamp", "timestamp"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    incident_id: int = Field(index=True, description="关联事件 ID")

    event_type: str = Field(
        description="事件类型：alert / heal / user / notification / system"
    )
    event_subtype: str = Field(description="事件子类型，如 alert.firing")

    title: str = Field(description="事件标题")
    description: Optional[str] = Field(default=None, description="事件描述")
    event_metadata: str = Field(
        default="{}",
        description="JSON 格式的额外上下文",
    )

    source: str = Field(
        default="system",
        description="来源：detector / executor / notifier / user / system",
    )
    created_by: Optional[int] = Field(default=None, description="用户操作时记录用户 ID")

    timestamp: datetime = Field(default_factory=now_utc, description="事件发生时间")
    created_at: datetime = Field(default_factory=now_utc, description="记录创建时间")
