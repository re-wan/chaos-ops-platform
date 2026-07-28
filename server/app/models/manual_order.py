"""本地人工订单模型（人工收款通道）。

人工收款（父母对公账户/微信/支付宝）确认到账后，手动录入订单号+邮箱，
客户即可凭此在自助页生成 License——与 Paddle 自动收款并行不冲突。
"""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class ManualOrder(SQLModel, table=True):
    """本地人工订单记录。"""

    __tablename__ = "manual_orders"

    id: int | None = Field(default=None, primary_key=True)
    order_id: str = Field(unique=True, index=True, description="人工订单号（如 manual_20260728_001）")
    email: str = Field(description="客户邮箱")
    note: str = Field(default="", description="备注（金额/渠道/客户信息）")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
