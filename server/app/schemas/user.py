"""用户管理请求/响应 Schema。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

VALID_ROLES = {"admin", "viewer"}


class UserCreate(BaseModel):
    """创建用户请求体。"""

    username: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="用户名，全局唯一",
    )
    email: Optional[EmailStr] = Field(
        default=None,
        description="邮箱，用于密码重置等通知；推荐填写",
    )
    password: str = Field(
        ...,
        min_length=6,
        max_length=128,
        description="登录密码",
    )
    role: str = Field(default="viewer", description="角色：admin / viewer")
    is_active: bool = Field(default=True, description="是否启用")

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"role 必须是 {VALID_ROLES} 之一")
        return v


class UserUpdate(BaseModel):
    """更新用户请求体。"""

    email: Optional[EmailStr] = Field(default=None, description="邮箱")
    password: Optional[str] = Field(default=None, min_length=6, max_length=128)
    role: Optional[str] = Field(default=None, description="角色：admin / viewer")
    is_active: Optional[bool] = Field(default=None)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_ROLES:
            raise ValueError(f"role 必须是 {VALID_ROLES} 之一")
        return v


class UserRead(BaseModel):
    """用户响应模型。"""

    id: int
    username: str
    email: Optional[str]
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
