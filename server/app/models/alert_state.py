"""告警规则状态（AlertState）数据模型。"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class AlertState(SQLModel, table=True):
    """告警规则在每个节点上的当前状态。"""

    __tablename__ = "alert_states"
    __table_args__ = (
        Index("ix_alert_states_rule_node", "rule_id", "node_id"),
        # Phase 3 Step 05：按节点+状态过滤、按最近通知时间排序的常用查询。
        Index("ix_alert_states_node_state", "node_id", "state"),
        Index("ix_alert_states_last_notified", "last_notified_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    rule_id: int = Field(index=True, description="关联告警规则 ID")
    node_id: str = Field(index=True, description="节点 ID")
    state: str = Field(default="idle", description="状态：idle/pending/firing/resolved")

    pending_since: Optional[datetime] = Field(
        default=None,
        description="进入 pending 状态的时间",
    )
    last_met_at: Optional[datetime] = Field(
        default=None,
        description="条件最近一次满足的时间",
    )
    last_not_met_at: Optional[datetime] = Field(
        default=None,
        description="条件最近一次不满足的时间",
    )
    last_notified_at: Optional[datetime] = Field(
        default=None,
        description="上次发送通知的时间，用于去重窗口",
    )

    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
