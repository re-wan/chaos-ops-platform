"""优化建议（OptimizationSuggestion）数据模型。

Phase 3 Step 03（自学习/演化 - 生命周期管理）。

本模型把 STEP_01 分析引擎（``services.optimizer``）产出的内存结构
``SuggestionDraft`` 落库，并承载其完整生命周期：

    pending ──apply──▶ applied ──rollback──▶ rolled_back
        │
        └──reject──▶ rejected

安全底线（务必遵守）：
- 仅人工确认后才 ``apply``；状态机非法迁移直接抛错（fail-closed）。
- ``snapshot_json`` 保存 apply 前目标配置的**完整快照**，粒度必须足以精确还原，
  保证 apply / rollback 对称。
- rolled_back / rejected 为终态：rolled_back 不可再次 apply，避免配置被反复切换。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.core.utils import now_utc

# 与 STEP_01 ``optimizer.SuggestionType`` 保持一致。
VALID_SUGGESTION_TYPES = {
    "alert_threshold",
    "heal_rule",
    "check_frequency",
    "retention_policy",
}

# 生命周期状态。
VALID_SUGGESTION_STATUSES = {"pending", "applied", "rejected", "rolled_back"}

# 显式状态机：任何不在表内的迁移均视为非法（fail-closed）。
# - pending 可确认应用或拒绝；
# - applied 仅可回滚；
# - rejected / rolled_back 为终态，不再迁出（rolled_back 不可再次 apply）。
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"applied", "rejected"},
    "applied": {"rolled_back"},
    "rejected": set(),
    "rolled_back": set(),
}


class OptimizationStatusError(ValueError):
    """非法状态值或非法状态迁移。"""


class OptimizationSuggestion(SQLModel, table=True):
    """优化建议表。

    每一行代表一条由分析引擎发现、待人工确认的优化建议。确认应用前会自动把
    目标配置快照写入 ``snapshot_json``，用于后续一键回滚精确还原。
    """

    __tablename__ = "optimization_suggestions"

    id: Optional[int] = Field(default=None, primary_key=True)
    type: str = Field(
        index=True,
        description=(
            "优化类型：alert_threshold / heal_rule / "
            "check_frequency / retention_policy"
        ),
    )
    target_id: str = Field(
        index=True,
        description="优化目标标识，如 alert_rule:5 / heal_rule:3 / "
        "check:node:metric / metric_retention:node:metric",
    )

    current_value: str = Field(description="当前配置/取值的人类可读描述")
    suggested_value: str = Field(description="建议配置/取值的人类可读描述")
    reason: str = Field(description="数据依据：统计结果或 LLM 生成的自然语言")
    expected_effect: Optional[str] = Field(
        default=None, description="预期收益（可选）"
    )
    confidence: float = Field(default=0.0, description="置信度 0~1")

    status: str = Field(
        default="pending",
        index=True,
        description="状态：pending / applied / rejected / rolled_back",
    )

    # apply 前的目标配置快照（JSON 字符串），粒度足以精确还原。
    # 未 apply 时为 "{}"。结构包含 ``_kind`` 与（可选的）``_metric_baseline``。
    snapshot_json: str = Field(
        default="{}",
        description="apply 前目标配置快照（JSON），用于一键回滚精确还原",
    )

    applied_by: Optional[int] = Field(default=None, description="应用人用户 ID")
    applied_at: Optional[datetime] = Field(default=None, description="应用时间")
    rolled_back_at: Optional[datetime] = Field(default=None, description="回滚时间")

    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------

    def transition_to(self, new_status: str) -> None:
        """显式状态迁移，非法迁移抛 ``OptimizationStatusError``。

        允许：
        - pending -> applied / rejected
        - applied -> rolled_back

        不允许：
        - rolled_back / rejected 的任意迁出（终态）
        - 任何状态 -> pending（草稿一旦落库不可重置为 pending）
        """
        if new_status not in VALID_SUGGESTION_STATUSES:
            raise OptimizationStatusError(f"非法状态值: {new_status}")
        if new_status == "pending":
            raise OptimizationStatusError("不允许回退到 pending")
        allowed = _ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise OptimizationStatusError(
                f"非法状态迁移: {self.status} -> {new_status}"
            )
        self.status = new_status
        self.updated_at = now_utc()
