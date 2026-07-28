"""节点组请求/响应 Schema。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class NodeGroupCreate(BaseModel):
    """创建节点组请求体。"""

    name: str = Field(..., min_length=1, max_length=128, description="节点组名称")
    description: Optional[str] = Field(default=None, description="节点组描述")
    node_ids: list[str] = Field(
        default_factory=list,
        description="节点业务 ID（node_id）列表",
    )

    @field_validator("node_ids")
    @classmethod
    def validate_node_ids(cls, v: list[str]) -> list[str]:
        if not isinstance(v, list):
            raise ValueError("node_ids 必须是数组")
        return v


class NodeGroupUpdate(BaseModel):
    """更新节点组请求体。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = None
    node_ids: Optional[list[str]] = None

    @field_validator("node_ids")
    @classmethod
    def validate_node_ids(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        if not isinstance(v, list):
            raise ValueError("node_ids 必须是数组")
        return v


class NodeGroupRead(BaseModel):
    """节点组响应模型。"""

    id: int
    name: str
    description: Optional[str]
    node_ids: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
