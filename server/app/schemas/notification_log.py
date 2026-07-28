"""通知发送记录 Schema。"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class NotificationLogRead(BaseModel):
    """通知发送记录响应模型。"""

    id: int
    channel_id: int
    event_type: str
    event_id: Optional[str]
    status: str
    payload: dict[str, Any]
    retry_count: int
    max_retries: int
    error_message: Optional[str]
    scheduled_at: Optional[datetime]
    sent_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
