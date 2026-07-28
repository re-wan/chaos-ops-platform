"""自愈任务（HealTask）数据模型。"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


def _generate_task_id() -> str:
    """生成业务任务 ID。"""
    import uuid

    return f"ht_{uuid.uuid4().hex[:16]}"


class HealTask(SQLModel, table=True):
    """自愈任务表。

    记录一次自愈执行的完整生命周期与审计信息。
    """

    __tablename__ = "heal_tasks"
    __table_args__ = (
        Index("ix_heal_tasks_node_status", "node_id", "status"),
        Index("ix_heal_tasks_status_created", "status", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(
        index=True,
        unique=True,
        default_factory=_generate_task_id,
        description="业务任务 ID",
    )
    node_id: str = Field(index=True, description="目标节点 ID")
    alert_rule_id: int = Field(description="触发告警的规则 ID")
    heal_rule_id: int = Field(description="触发的自愈规则 ID")
    action_id: str = Field(description="执行的动作标识")
    action_params: str = Field(description="渲染后的动作参数 JSON")
    status: str = Field(
        default="pending",
        description="任务状态：pending/approved/running/success/failed/rejected/timeout",
    )
    risk_level: str = Field(description="动作风险等级")
    requires_approval: bool = Field(
        default=True,
        description="是否需要人工确认",
    )
    approved_by: Optional[int] = Field(default=None, description="确认人 user_id")
    approved_at: Optional[datetime] = Field(default=None, description="确认时间")
    executed_by: Optional[str] = Field(
        default=None,
        description="执行者：agent_id 或 manual",
    )
    started_at: Optional[datetime] = Field(default=None, description="开始执行时间")
    finished_at: Optional[datetime] = Field(default=None, description="完成时间")
    result: Optional[str] = Field(default=None, description="执行结果 JSON")
    error_message: Optional[str] = Field(default=None, description="错误信息")

    # 验证相关字段
    verification_config: Optional[str] = Field(
        default=None, description="验证配置 JSON"
    )
    verification_status: Optional[str] = Field(
        default=None, description="验证状态：pending/success/failed"
    )
    verification_result: Optional[str] = Field(
        default=None, description="验证结果 JSON"
    )
    verification_due_at: Optional[datetime] = Field(
        default=None, description="验证应开始时间"
    )

    # 重试相关字段
    retry_count: int = Field(default=0, description="已重试次数")
    max_retries: int = Field(default=2, description="最大重试次数")
    scheduled_at: Optional[datetime] = Field(
        default=None, description="下次可执行时间（用于重试间隔）"
    )

    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
