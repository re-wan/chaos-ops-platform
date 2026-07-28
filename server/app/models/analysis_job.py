"""AI 根因分析任务队列表。

事件创建时自动入队（可开关），后台消费者串行执行（并发=1），
配合 AIMD 自适应限流控制 LLM 调用速率。状态机：
pending -> running -> success / failed；skipped 为终态（入队即置，带原因）。
失败不自动重试，管理员可在事件详情页手动重新分析。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class AnalysisJob(SQLModel, table=True):
    """AI 根因分析任务（事件级去重：同 incident 仅允许一个 pending/running）。"""

    __tablename__ = "analysis_jobs"
    __table_args__ = (
        Index("ix_analysis_jobs_status_created", "status", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    incident_id: int = Field(index=True, description="关联事件 ID")
    status: str = Field(
        default="pending",
        description="pending/running/success/failed/skipped",
    )
    reason: Optional[str] = Field(
        default=None, description="失败/跳过原因（动态文本，不翻译）"
    )

    created_at: datetime = Field(default_factory=now_utc, description="入队时间")
    started_at: Optional[datetime] = Field(default=None, description="开始执行时间")
    finished_at: Optional[datetime] = Field(default=None, description="完成时间")
