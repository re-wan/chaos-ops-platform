"""License 自助生成（claim）记录数据模型。

客户凭「订单号 + 购买邮箱 + install_id」免登录自助生成绑定 License，
本表记录每一次成功生成，用于：
- 生成次数硬上限（order_id 唯一约束，每订单最多生成 1 次）；
- 一次性下载令牌（24h 有效、单次使用）；
- 审计追溯（IP、时间、邮箱、install_id）。
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class LicenseClaim(SQLModel, table=True):
    """License 自助生成记录表。"""

    __tablename__ = "license_claims"

    id: Optional[int] = Field(default=None, primary_key=True)
    # 生成次数硬上限：同一订单号最多生成 1 次（数据库唯一约束兜底并发）
    order_id: str = Field(max_length=64, unique=True, index=True)
    email: str = Field(max_length=254)
    install_id: str = Field(max_length=64)
    license_id: str = Field(max_length=64, unique=True, index=True)
    # 一次性下载令牌（生成时签发，24h 有效，单次使用）
    download_token: str = Field(max_length=128)
    token_expires_at: datetime
    token_used: bool = Field(default=False)
    # 签名后的 License 文件内容（JSON 文本），下载时原样返回；
    # 私钥永不出服务端，本字段只是签名结果，非敏感材料
    license_json: str = ""
    ip_address: Optional[str] = Field(default=None, max_length=45)
    created_at: datetime = Field(default_factory=now_utc)
