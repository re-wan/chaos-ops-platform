"""User（管理员用户）数据模型。"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class User(SQLModel, table=True):
    """管理员用户表。

    MVP 阶段仅支持单一管理员角色；role 与 tenant_id 为 Phase 2/3 预留字段。
    """

    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    email: Optional[str] = Field(default=None, index=True, unique=True)
    hashed_password: str
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=now_utc)

    # Phase 2/3 预留字段
    role: str = Field(default="admin")
    tenant_id: Optional[int] = Field(default=None)
