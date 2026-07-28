"""AI 生成自定义工具（AIGeneratedTool）数据模型。

企业版专属：AI 根据故障上下文生成的自定义修复脚本草稿。

安全底线（务必遵守）：
- 生成的脚本**绝不自动执行**、**绝不自动进入动作库**；初始状态恒为 ``pending_review``。
- ``content_hash = sha256(content)`` 在创建/更新时计算，执行前由 Agent 双重校验。
- 内容一旦变更，hash 变化后必须回到 ``pending_review`` 重新审批（已审批状态作废）。
- 状态机仅允许合法迁移；非法迁移直接抛错（fail-closed）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.core.utils import now_utc

# 允许的语言与状态/风险枚举（与 schemas/服务层保持一致）
VALID_LANGUAGES = {"python", "shell"}
VALID_TOOL_STATUSES = {"pending_review", "approved", "rejected", "deprecated"}
VALID_RISK_LEVELS = {"low", "medium", "high"}

# 状态机：允许的显式迁移。
# 注意：任何状态 -> pending_review 的“回退”不允许通过 transition_to 直接触发，
# 必须经由 mark_content_changed()（内容变更导致 hash 变化）这一唯一受信路径。
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending_review": {"approved", "rejected"},
    "approved": {"deprecated"},
    "rejected": set(),
    "deprecated": set(),
}


class ToolStatusError(ValueError):
    """非法状态迁移或非法状态值。"""


class AIGeneratedTool(SQLModel, table=True):
    """AI 生成的自定义工具表。

    每一行代表一个由 AI 生成、待人工审批的修复脚本草稿。审批通过后由服务层
    同步创建/更新一条 ``HealAction(action_type="ai_generated_script")`` 进入动作库，
    再由现有 HEAL_EXECUTOR / Agent 链路执行（复用 script_hash 双重校验）。
    """

    __tablename__ = "ai_generated_tools"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(description="工具显示名称")
    description: Optional[str] = Field(default=None, description="工具说明/预期效果")
    language: str = Field(description="脚本语言：python / shell")
    content: str = Field(description="脚本内容（已落库但绝不自动执行）")
    content_hash: str = Field(description="脚本内容 SHA256 hash")

    source_incident_id: Optional[int] = Field(
        default=None, index=True, description="触发生成的事件 ID"
    )
    source_analysis_id: Optional[int] = Field(
        default=None, index=True, description="关联的 AI 分析记录 ID"
    )
    generated_by_ai: str = Field(
        default="unknown", description="生成该脚本的 LLM 模型标识"
    )
    generated_at: datetime = Field(default_factory=now_utc, description="生成时间")

    risk_level: str = Field(
        default="high", description="风险等级：low / medium / high"
    )
    status: str = Field(
        default="pending_review",
        description="状态：pending_review / approved / rejected / deprecated",
    )
    approved_by: Optional[int] = Field(default=None, description="审批人用户 ID")
    approved_at: Optional[datetime] = Field(default=None, description="审批时间")

    # 关联动作库：审批通过后写入对应 HealAction.action_id，便于审计追溯。
    linked_action_id: Optional[str] = Field(
        default=None, description="审批通过后关联的 HealAction.action_id"
    )

    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")

    # ------------------------------------------------------------------
    # Hash 工具
    # ------------------------------------------------------------------

    @staticmethod
    def compute_hash(content: str) -> str:
        """计算脚本内容 SHA256 hash（与 Agent 端 ``_compute_script_hash`` 同算法）。"""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def verify_hash(self) -> bool:
        """校验当前 ``content`` 与 ``content_hash`` 是否一致。"""
        return self.compute_hash(self.content) == self.content_hash

    def recompute_hash(self) -> None:
        """根据当前 ``content`` 重算并写回 ``content_hash``。"""
        self.content_hash = self.compute_hash(self.content)

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------

    def transition_to(self, new_status: str) -> None:
        """显式状态迁移，非法迁移抛 ``ToolStatusError``（fail-closed）。

        允许：
        - pending_review -> approved / rejected
        - approved -> deprecated

        不允许：
        - 任何状态 -> pending_review（必须走 ``mark_content_changed``）
        - rejected / deprecated 的任意迁出
        """
        if new_status not in VALID_TOOL_STATUSES:
            raise ToolStatusError(f"非法状态值: {new_status}")
        if new_status == "pending_review":
            raise ToolStatusError(
                "不允许直接回退到 pending_review，需通过内容变更触发重新审批"
            )
        allowed = _ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ToolStatusError(
                f"非法状态迁移: {self.status} -> {new_status}"
            )
        self.status = new_status
        self.updated_at = now_utc()

    def mark_content_changed(self, new_content: str) -> bool:
        """应用内容变更：重算 hash；若 hash 变化则强制回到 pending_review 并清空审批。

        Returns:
            True 表示内容确实发生了变化（hash 改变）；False 表示内容未变。
        """
        new_hash = self.compute_hash(new_content)
        if new_hash == self.content_hash:
            return False
        self.content = new_content
        self.content_hash = new_hash
        # 内容变更后，无论之前是 approved/rejected，都必须重新审批。
        self.status = "pending_review"
        self.approved_by = None
        self.approved_at = None
        self.linked_action_id = None
        self.updated_at = now_utc()
        return True
