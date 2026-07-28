"""自愈规则（HealRule）数据模型。"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class HealRule(SQLModel, table=True):
    """自愈规则表。

    定义告警规则与自愈动作之间的映射关系。
    """

    __tablename__ = "heal_rules"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(description="规则名称")
    description: Optional[str] = Field(default=None, description="规则说明")
    alert_rule_id: int = Field(index=True, description="关联的告警规则 ID")
    action_id: str = Field(index=True, description="关联的自愈动作标识")
    action_params: str = Field(
        default="{}",
        description="动作参数 JSON，支持模板变量",
    )
    enabled: bool = Field(default=True, description="是否启用")
    auto_execute: bool = Field(
        default=False,
        description="是否自动执行（仍需受白名单/高危策略约束）",
    )
    priority: int = Field(default=0, description="优先级，Phase 2 使用")
    verification_config: Optional[str] = Field(
        default=None, description="验证配置 JSON"
    )
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
