"""密码重置请求/响应 Schema。"""

from pydantic import BaseModel, Field


class PasswordResetRequest(BaseModel):
    """请求密码重置。"""

    username: str = Field(..., min_length=1, max_length=64, description="用户名")


class PasswordResetConfirm(BaseModel):
    """使用 token 重置密码。"""

    token: str = Field(..., min_length=1, description="重置令牌")
    new_password: str = Field(
        ...,
        min_length=6,
        max_length=128,
        description="新密码",
    )


class PasswordResetResponse(BaseModel):
    """统一响应消息。"""

    message: str
