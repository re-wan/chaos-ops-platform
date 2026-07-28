"""远程命令执行（RemoteExecution）数据模型。"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


def _generate_execution_id() -> str:
    """生成业务执行 ID。"""
    return f"re_{uuid.uuid4().hex[:16]}"


class RemoteExecution(SQLModel, table=True):
    """远程命令执行审计记录表。

    记录管理员通过 Web 控制台在节点上执行白名单命令的完整生命周期。
    """

    __tablename__ = "remote_executions"
    __table_args__ = (
        Index("ix_remote_executions_node_status", "node_id", "status"),
        Index("ix_remote_executions_created_at", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    execution_id: str = Field(
        index=True,
        unique=True,
        default_factory=_generate_execution_id,
        description="业务执行 ID",
    )
    node_id: str = Field(index=True, description="目标节点 ID")
    user_id: int = Field(description="触发执行的用户 ID")
    action_id: str = Field(description="白名单动作标识")
    action_params: str = Field(description="动作参数 JSON")
    status: str = Field(
        default="pending",
        description="状态：pending/running/success/failed/timeout",
    )
    output: Optional[str] = Field(default=None, description="执行输出（已脱敏）")
    error_message: Optional[str] = Field(default=None, description="错误信息")
    timeout_seconds: int = Field(
        default=60,
        description="执行超时秒数",
    )
    started_at: Optional[datetime] = Field(default=None, description="开始执行时间")
    finished_at: Optional[datetime] = Field(default=None, description="完成时间")
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
