"""UserSession（用户会话）数据模型。"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class UserSession(SQLModel, table=True):
    """用户会话表。

    Token 使用服务端 Session 机制，支持撤销、滑动过期与绝对过期。
    """

    __tablename__ = "user_sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    token: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=now_utc)
    last_used_at: datetime = Field(default_factory=now_utc)
    expires_at: datetime
    is_active: bool = Field(default=True)
