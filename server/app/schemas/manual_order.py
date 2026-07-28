"""本地人工订单 Schema（人工收款通道）。"""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class ManualOrderCreate(BaseModel):
    """录入人工订单。

    order_id 校验规则与 License 自助生成页输入一致（正则白名单），
    避免录入了客户在自助页无法使用的订单号。
    """

    order_id: str = Field(
        min_length=5,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="人工订单号（如 manual_20260728_001）",
    )
    email: EmailStr = Field(max_length=254, description="客户邮箱")
    note: str = Field(default="", max_length=500, description="备注（金额/渠道/客户信息）")


class ManualOrderRead(BaseModel):
    """人工订单展示。"""

    id: int
    order_id: str
    email: str
    note: str
    created_at: datetime
