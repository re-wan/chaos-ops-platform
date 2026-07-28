"""优化效果追踪（OptimizationEffect）数据模型。

Phase 3 Step 03（自学习/演化 - 生命周期管理）。

每条记录对应一条已 ``applied`` 建议在追踪窗口（默认 7 天）到期后的效果对比：
``before_value``（apply 时刻基线） vs ``after_value``（追踪到期测量）。
用于判断优化是否生效，辅助决定保留还是一键回滚。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class OptimizationEffect(SQLModel, table=True):
    """优化效果追踪表。

    例如 ``metric_name=false_positive_rate``，``before_value=0.9``，
    ``after_value=0.2``，表示误报率从 90% 降至 20%。
    """

    __tablename__ = "optimization_effects"

    id: Optional[int] = Field(default=None, primary_key=True)
    suggestion_id: int = Field(
        index=True, description="关联的优化建议 ID"
    )
    metric_name: str = Field(
        description="效果指标名，如 false_positive_rate / heal_success_rate / "
        "check_interval_seconds / retention_days"
    )
    before_value: float = Field(description="apply 时刻的指标基线值")
    after_value: float = Field(description="追踪窗口到期时的指标测量值")
    measured_at: datetime = Field(
        default_factory=now_utc, description="效果测量时间"
    )
