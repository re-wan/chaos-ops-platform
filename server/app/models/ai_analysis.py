"""AI 根因分析结果数据模型。"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class AIAnalysis(SQLModel, table=True):
    """AI 根因分析记录表。

    每次对 Incident 调用分析都会生成一条记录，保存 LLM 原始响应、解析后的建议动作
    以及是否已执行、执行结果。
    """

    __tablename__ = "ai_analysis"

    id: Optional[int] = Field(default=None, primary_key=True)
    incident_id: int = Field(index=True, description="关联事件 ID")
    prompt: str = Field(description="发送给 LLM 的完整 Prompt")
    response: Optional[str] = Field(default=None, description="LLM 原始返回内容")
    analysis: Optional[str] = Field(default=None, description="根因分析结论")
    reasoning: Optional[str] = Field(default=None, description="推理过程")
    suggested_action: Optional[str] = Field(default=None, description="建议执行的动作 ID")
    action_params: Optional[str] = Field(default=None, description="建议动作参数（JSON 字符串）")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="置信度 0-1")
    executed: bool = Field(default=False, description="是否已执行建议动作")
    execution_result: Optional[str] = Field(default=None, description="执行结果摘要")
    heal_task_id: Optional[str] = Field(default=None, description="关联自愈任务 task_id")
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
