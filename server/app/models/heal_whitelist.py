"""自愈自动执行白名单（HealAutoApproveWhitelist）数据模型。"""

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class HealAutoApproveWhitelist(SQLModel, table=True):
    """自愈自动执行白名单表。

    按 (node_id, action_id) 维护，每个节点独立。
    """

    __tablename__ = "heal_whitelist"
    __table_args__ = (
        UniqueConstraint("node_id", "action_id", name="uq_heal_whitelist_node_action"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    node_id: str = Field(index=True, description="节点 ID")
    action_id: str = Field(index=True, description="动作标识")
    enabled: bool = Field(default=True, description="是否启用")
    created_by: Optional[int] = Field(default=None, description="创建人 user_id")
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
