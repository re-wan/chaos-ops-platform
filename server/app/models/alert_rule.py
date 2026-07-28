"""告警规则（AlertRule）数据模型。"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel
from app.core.utils import now_utc

class AlertRule(SQLModel, table=True):
    """告警规则表。

    定义告警条件、范围、持续时间和严重度，供告警检测引擎消费。
    规则删除采用硬删除；历史告警记录由独立表维护，不随规则级联删除。
    """

    __tablename__ = "alert_rules"
    # SQLite 默认在删除最大 id 行后复用该 rowid，会导致 HealRule.alert_rule_id、
    # OptimizationSuggestion.target_id（"alert_rule:N"）等按 id 引用的悬挂行错绑到
    # 新建规则上（误 apply/误回滚）。AUTOINCREMENT 保证 id 单调递增、永不复用；
    # PostgreSQL 会忽略该参数（序列天然不复用 id）。
    __table_args__ = {"sqlite_autoincrement": True}

    id: Optional[int] = Field(default=None, primary_key=True, description="数据库主键")
    name: str = Field(
        index=True,
        unique=True,
        max_length=128,
        description="规则名称，全局唯一",
    )
    description: Optional[str] = Field(default=None, description="规则描述")

    scope: str = Field(description="规则范围：global / node / node_group")
    scope_target: Optional[str] = Field(
        default=None,
        description="范围目标：node_id 或 group_id",
    )

    condition_type: str = Field(description="条件语法类型：json_dsl / promql")
    condition: str = Field(description="条件内容：JSON 字符串或 PromQL 表达式")

    pending_duration_seconds: int = Field(
        default=60,
        description="条件持续满足多久后进入 firing",
    )
    resolve_duration_seconds: int = Field(
        default=60,
        description="条件恢复持续多久后标记为 resolved",
    )

    severity: str = Field(description="告警严重度：critical / warning / info")
    enabled: bool = Field(default=True, description="是否启用")

    notification_channel_ids: str = Field(
        default="[]",
        description="通知渠道 ID 列表（JSON 数组）",
    )

    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
