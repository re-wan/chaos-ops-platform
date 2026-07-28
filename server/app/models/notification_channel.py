"""通知渠道数据模型。

支持 Email、Webhook 及 Phase 2 的 IM 渠道扩展。
配置以 JSON 字符串加密存储。
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class NotificationChannel(SQLModel, table=True):
    """通知渠道表。

    每个渠道对应一种通知后端（email/webhook/dingtalk 等），
    配置中的敏感字段会被加密后存储。
    """

    __tablename__ = "notification_channels"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(description="渠道名称")
    channel_type: str = Field(
        description="渠道类型：email / webhook / dingtalk / wecom / lark / slack"
    )
    config: str = Field(description="JSON 配置，加密后存储")
    enabled: bool = Field(default=True, description="是否启用")
    lang: str = Field(default="zh", description="通知语言：zh / en")
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
