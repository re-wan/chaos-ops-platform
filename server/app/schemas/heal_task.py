"""自愈任务请求/响应 Schema。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class HealTaskRead(BaseModel):
    """自愈任务响应模型。"""

    id: int
    task_id: str
    node_id: str
    alert_rule_id: int
    heal_rule_id: int
    action_id: str
    action_params: dict
    status: str
    risk_level: str
    requires_approval: bool
    approved_by: Optional[int]
    approved_at: Optional[datetime]
    executed_by: Optional[str]
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    result: Optional[dict]
    error_message: Optional[str]
    verification_config: Optional[dict]
    verification_status: Optional[str]
    verification_result: Optional[dict]
    verification_due_at: Optional[datetime]
    retry_count: int
    max_retries: int
    scheduled_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class HealTaskVerificationRead(BaseModel):
    """验证结果查看模型。"""

    verification_status: Optional[str]
    verification_result: Optional[dict]
    retry_count: int
    max_retries: int

    model_config = {"from_attributes": True}


class HealTaskResultUpdate(BaseModel):
    """Agent 上报任务结果请求体。"""

    success: bool = Field(description="是否执行成功")
    output: Optional[str] = Field(default=None, description="执行输出")
    message: Optional[str] = Field(default=None, description="结果说明")
    error_message: Optional[str] = Field(default=None, description="错误信息")
