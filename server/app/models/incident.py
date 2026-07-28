"""事件（Incident）数据模型。

Step 15 扩展为完整生命周期版本，支持自动创建、告警聚合、状态流转、手动管理。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class Incident(SQLModel, table=True):
    """事件表。

    一个 Incident 聚合同一 (rule_id, node_id) 时间窗口内的多次告警 firing，
    并支持 open / acknowledged / resolved / closed 状态流转。
    """

    __tablename__ = "incidents"
    __table_args__ = (
        Index("ix_incidents_rule_node_status", "rule_id", "node_id", "status"),
        Index("ix_incidents_status_updated", "status", "updated_at"),
        # Phase 3 Step 05：按事件开始时间排序/范围查询。
        Index("ix_incidents_started_at", "started_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(description="事件标题")
    description: Optional[str] = Field(default=None, description="事件描述")
    severity: str = Field(description="严重度：critical / warning / info")
    status: str = Field(default="open", description="状态：open / acknowledged / resolved / closed")

    source: str = Field(default="auto", description="来源：auto / manual")
    created_by: Optional[int] = Field(default=None, description="手动创建时记录用户 ID")
    assigned_to: Optional[int] = Field(default=None, description="负责人用户 ID")

    # 聚合维度：保留 rule_id 和 node_id 作为快捷字段，便于按维度查询
    rule_id: Optional[int] = Field(default=None, index=True, description="关联告警规则 ID")
    node_id: Optional[str] = Field(default=None, index=True, description="节点 ID")

    started_at: datetime = Field(description="事件开始时间")
    acknowledged_at: Optional[datetime] = Field(default=None, description="认领时间")
    resolved_at: Optional[datetime] = Field(default=None, description="解决时间")
    closed_at: Optional[datetime] = Field(default=None, description="关闭时间")

    is_deleted: bool = Field(default=False, description="是否已软删除")

    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
