"""告警静默规则数据模型。"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class AlertSilence(SQLModel, table=True):
    """告警静默规则表。

    当告警 labels 完全匹配 matchers 中所有键值对，且当前时间位于
    [starts_at, ends_at] 区间内时，该告警被静默，不发送通知。
    """

    __tablename__ = "alert_silences"

    id: Optional[int] = Field(default=None, primary_key=True)
    matchers: str = Field(
        description='JSON 对象，如 {"node_id": "node_abc", "severity": "warning"}',
    )
    starts_at: datetime = Field(description="静默生效开始时间")
    ends_at: datetime = Field(description="静默失效时间")
    comment: Optional[str] = Field(default=None, description="静默说明")
    created_by: Optional[int] = Field(
        default=None,
        description="创建者 user_id，Phase 2 接入用户体系",
    )
    created_at: datetime = Field(
        default_factory=now_utc,
        description="创建时间",
    )
