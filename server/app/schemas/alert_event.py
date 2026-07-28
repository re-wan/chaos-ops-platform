"""告警事件请求/响应 Schema。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class AlertEventRead(BaseModel):
    """告警事件响应模型。"""

    id: int
    rule_id: int
    node_id: str
    severity: str
    message: Optional[str]
    labels: Optional[dict]
    fired_at: datetime

    model_config = {"from_attributes": True}
