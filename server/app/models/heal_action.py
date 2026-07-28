"""自愈动作（HealAction）数据模型。"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class HealAction(SQLModel, table=True):
    """自愈动作库表。

    定义可在 Agent 节点上执行的自愈动作模板，包括预置动作和自定义脚本。
    自定义脚本需经 admin 审批后才能执行。
    """

    __tablename__ = "heal_actions"

    id: Optional[int] = Field(default=None, primary_key=True)
    action_id: str = Field(
        index=True,
        unique=True,
        description="唯一标识，如 restart_service",
    )
    name: str = Field(description="动作显示名称")
    description: Optional[str] = Field(default=None, description="动作说明")
    action_type: str = Field(
        description="动作类型：builtin / custom_script（Phase 3 支持 ai_generated_script）",
    )
    script_content: Optional[str] = Field(
        default=None,
        description="自定义脚本内容，builtin 动作为空",
    )
    script_hash: Optional[str] = Field(
        default=None,
        description="脚本内容 SHA256 hash，用于防篡改校验",
    )
    interpreter: str = Field(
        default="bash",
        description="脚本解释器：bash / python / python3",
    )
    parameter_schema: str = Field(
        description="动作参数 JSON Schema",
    )
    risk_level: str = Field(
        description="风险等级：low / medium / high",
    )
    is_approved: bool = Field(
        default=False,
        description="自定义脚本是否已审批",
    )
    is_builtin: bool = Field(
        default=False,
        description="是否为预置动作",
    )
    snapshot_target_field: Optional[str] = Field(
        default=None,
        description="AI 快照优先读取的参数字段名",
    )
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
