"""批量注册任务数据模型。"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class BulkRegistrationTask(SQLModel, table=True):
    """批量注册任务记录。

    记录一次批量创建节点的总数、成功数、失败数以及每行详细结果。
    """

    __tablename__ = "bulk_registration_tasks"

    id: Optional[int] = Field(default=None, primary_key=True, description="任务 ID")
    total_count: int = Field(description="总节点数")
    success_count: int = Field(description="成功创建的节点数")
    failure_count: int = Field(description="创建失败的节点数")
    details: str = Field(description="JSON 数组，记录每个节点的处理结果")
    created_by: Optional[int] = Field(
        default=None,
        foreign_key="users.id",
        description="执行批量操作的用户 ID",
    )
    created_at: datetime = Field(default_factory=now_utc, description="任务创建时间")
