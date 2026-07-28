"""通知发送记录数据模型。

记录每条通知的创建、发送、重试和最终状态，用于审计和失败重试。
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class NotificationLog(SQLModel, table=True):
    """通知发送记录表。

    状态机：pending -> success / failed。
    失败时按指数退避策略重试，达到上限后标记为 failed。
    """

    __tablename__ = "notification_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(index=True, description="关联通知渠道 ID")
    event_type: str = Field(description="事件类型：alert / incident / test")
    event_id: Optional[str] = Field(default=None, description="关联事件的业务 ID")
    status: str = Field(default="pending", description="状态：pending / success / failed")
    payload: str = Field(description="发送内容 JSON")
    retry_count: int = Field(default=0, description="已重试次数")
    max_retries: int = Field(default=3, description="最大重试次数")
    error_message: Optional[str] = Field(default=None, description="失败错误信息")
    scheduled_at: Optional[datetime] = Field(default=None, description="下次重试时间")
    sent_at: Optional[datetime] = Field(default=None, description="最终发送时间")
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
