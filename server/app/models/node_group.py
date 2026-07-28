"""节点组（NodeGroup）数据模型。

Phase 1 最小实现：仅支持静态节点 ID 列表，用于告警规则 node_group scope。
Phase 2 会扩展为支持标签表达式、动态成员关系等。
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class NodeGroup(SQLModel, table=True):
    """节点组表。

    通过 ``node_ids`` 字段存储节点业务 ID（node_id）字符串列表的 JSON 序列化结果。
    """

    __tablename__ = "node_groups"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True, description="节点组名称，全局唯一")
    description: Optional[str] = Field(default=None, description="节点组描述")
    node_ids: str = Field(default="[]", description="JSON 数组字符串，元素为 node_id")
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
