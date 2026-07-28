"""AI 自愈配置数据模型。

单例表，全局仅一条记录。默认关闭，需管理员手动开启并配置 LLM。
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class AIConfig(SQLModel, table=True):
    """AI 自愈全局配置表。"""

    __tablename__ = "ai_config"

    id: Optional[int] = Field(default=None, primary_key=True)
    enabled: bool = Field(default=False, description="是否启用 AI 根因分析")
    provider: str = Field(default="openai", description="LLM 提供商：openai / anthropic / ollama")
    model: str = Field(default="gpt-4o-mini", description="模型名称")
    api_key: Optional[str] = Field(default=None, description="API Key（加密存储）")
    api_base: Optional[str] = Field(default=None, description="自定义 API Base URL")
    auto_execute: bool = Field(default=False, description="是否自动执行白名单内建议动作")
    snapshot_enabled: bool = Field(default=True, description="自动执行前是否生成快照")
    snapshot_keep_count: int = Field(default=1, ge=0, description="保留快照数量")
    max_context_lines: int = Field(default=100, ge=0, description="Prompt 中最大日志/指标行数")
    auto_analyze: bool = Field(
        default=True, description="事件创建时是否自动入队 AI 根因分析"
    )
    rate_mode: str = Field(
        default="adaptive",
        description="分析限流模式：adaptive（AIMD 自适应）/ fixed（固定速率）",
    )
    fixed_rate: int = Field(
        default=2, ge=1, description="固定模式速率上限（次/分钟）"
    )
    max_rate: int = Field(
        default=10, ge=1, description="自适应模式速率上限（次/分钟）"
    )

    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
