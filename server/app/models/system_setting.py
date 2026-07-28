"""系统运行时配置表。

用于持久化可由管理员在 Web 控制台修改的运行时配置，
例如当前指标后端类型。环境变量提供默认值，运行时设置优先。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class SystemSetting(SQLModel, table=True):
    """系统设置表。

    以 key-value 形式存储，key 全局唯一，value 为字符串。
    复杂值可序列化为 JSON 字符串存储。
    """

    __tablename__ = "system_settings"

    key: str = Field(primary_key=True, description="设置键，全局唯一")
    value: str = Field(description="设置值，字符串形式")
    updated_at: datetime = Field(
        default_factory=now_utc,
        description="最后更新时间",
    )
