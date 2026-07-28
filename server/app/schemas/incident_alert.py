"""事件与告警关联 Schema。"""

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


class IncidentAlertRead(BaseModel):
    """事件与告警事件关联响应模型（包含告警事件详情）。"""

    id: int
    incident_id: int
    alert_event_id: int
    added_at: datetime
    alert_event: Optional[AlertEventRead] = None

    model_config = {"from_attributes": True}
