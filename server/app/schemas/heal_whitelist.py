"""自愈白名单请求/响应 Schema。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class HealWhitelistCreate(BaseModel):
    """创建白名单请求体。"""

    node_id: str = Field(..., min_length=1, description="节点 ID")
    action_id: str = Field(..., min_length=1, description="动作标识")


class HealWhitelistRead(BaseModel):
    """白名单响应模型。"""

    id: int
    node_id: str
    action_id: str
    enabled: bool
    created_by: Optional[int]
    created_at: datetime

    model_config = {"from_attributes": True}
