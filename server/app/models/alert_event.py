"""告警事件（AlertEvent）数据模型。

每次告警 firing 并通过通知决策时，创建一条 AlertEvent 记录，
供事件聚合与后续时间线回放使用。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class AlertEvent(SQLModel, table=True):
    """告警事件表。

    记录一次具体的告警 firing，包含规则、节点、严重度、消息与标签。
    """

    __tablename__ = "alert_events"
    # Phase 3 Step 05：按时间戳与常用过滤列建立复合索引，支撑大时间范围/按节点/按规则查询。
    __table_args__ = (
        Index("ix_alert_events_fired_at", "fired_at"),
        Index("ix_alert_events_rule_fired", "rule_id", "fired_at"),
        Index("ix_alert_events_node_fired", "node_id", "fired_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    rule_id: int = Field(index=True, description="关联告警规则 ID")
    node_id: str = Field(index=True, description="节点 ID")
    severity: str = Field(description="严重度：critical / warning / info")
    message: Optional[str] = Field(default=None, description="告警消息")
    labels: Optional[str] = Field(default=None, description="JSON 格式的标签")
    fired_at: datetime = Field(default_factory=now_utc, description="触发时间")
