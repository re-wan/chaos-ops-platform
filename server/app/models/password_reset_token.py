"""密码重置令牌数据模型。"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class PasswordResetToken(SQLModel, table=True):
    """密码重置令牌表。

    令牌一次性使用，15 分钟内有效。
    """

    __tablename__ = "password_reset_tokens"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    token: str = Field(index=True, unique=True)
    expires_at: datetime
    used: bool = Field(default=False)
    created_at: datetime = Field(default_factory=now_utc)
    used_at: Optional[datetime] = None
