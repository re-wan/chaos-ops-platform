"""通知模板数据模型。

支持按 event_type + channel_type 自定义模板，覆盖系统默认模板。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class NotificationTemplate(SQLModel, table=True):
    """通知模板表。

    每个 (event_type, channel_type) 组合可有一条自定义模板（is_default=False）
    和一条系统默认模板（is_default=True）。自定义模板优先于默认模板。
    """

    __tablename__ = "notification_templates"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(description="模板名称")
    event_type: str = Field(
        index=True,
        description="事件类型：alert.firing / alert.resolved / incident.created / ...",
    )
    channel_type: str = Field(
        index=True,
        description="渠道类型：email / webhook / dingtalk / ...",
    )
    subject_template: Optional[str] = Field(
        default=None, description="邮件主题模板，非邮件渠道可为空"
    )
    body_template: str = Field(description="正文模板")
    format: str = Field(
        default="text", description="输出格式：text / markdown / html"
    )
    is_default: bool = Field(
        default=False, description="是否为系统默认模板，默认模板不可删除"
    )
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")

    __table_args__ = (
        UniqueConstraint(
            "event_type",
            "channel_type",
            "is_default",
            name="uq_template_event_channel_default",
        ),
    )
