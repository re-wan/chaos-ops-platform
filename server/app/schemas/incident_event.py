"""事件时间线（IncidentEvent）Schema。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class IncidentEventRead(BaseModel):
    """时间线事件响应模型。"""

    id: int
    incident_id: int
    event_type: str
    event_subtype: str
    title: str
    description: Optional[str]
    source: str
    created_by: Optional[int]
    timestamp: datetime
    created_at: datetime
    event_metadata: Optional[dict]

    model_config = {"from_attributes": True}

    @field_validator("event_metadata", mode="before")
    @classmethod
    def parse_metadata(cls, v):
        """将 JSON 字符串解析为字典。"""
        import json

        if v is None:
            return None
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return None
        return None


class IncidentEventNoteCreate(BaseModel):
    """添加人工注释请求体。"""

    content: str = Field(..., min_length=1, max_length=2048, description="注释内容")
