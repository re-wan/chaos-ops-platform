"""告警抑制规则数据模型。"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class AlertInhibition(SQLModel, table=True):
    """告警抑制规则表。

    当 source_matchers 匹配到正在 firing 的父告警时，所有满足
    target_matchers 且与父告警在 equal_labels 指定标签上取值相同的
    子告警将被抑制，不再发送通知。
    """

    __tablename__ = "alert_inhibitions"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_matchers: str = Field(
        description="父告警匹配条件 JSON",
    )
    target_matchers: str = Field(
        description="被抑制告警匹配条件 JSON",
    )
    equal_labels: str = Field(
        description='必须相同的标签列表 JSON，如 ["node_id"]',
    )
    created_at: datetime = Field(
        default_factory=now_utc,
        description="创建时间",
    )
