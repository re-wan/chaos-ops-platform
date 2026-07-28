"""开放 API 相关数据模型（Phase 3 Step 04）。

包含两张表：

- ``ApiKey``：向外部系统开放 API 时使用的长期密钥。明文只在创建/轮转时
  返回一次，数据库仅持久化 ``sha256`` 哈希与前 8 位前缀。
- ``WebhookSubscription``：外部系统订阅 ChaosOps 内部事件（alert/incident/node）
  的回调配置。``secret`` 用于出站 HMAC 签名，同样只在创建时回显一次。

安全约定（见 ``docs/api/API_OPEN_DESIGN.md`` 第 8 节）：

- API Key / Webhook secret 的明文**绝不入库**，也**绝不出现在任何列表/详情接口**。
- ``scopes`` / ``events`` 以 JSON 字符串（TEXT）存储，读写走本模块提供的 helper。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.core.utils import now_utc

# 开放 API 支持的全部 scope 集合。读写分离：read:* 只读，write:* 需要 admin 角色。
VALID_API_SCOPES: frozenset[str] = frozenset(
    {
        "read:metrics",
        "read:alerts",
        "read:incidents",
        "read:nodes",
        "read:alert_rules",
        "write:alert_rules",
        "read:notification_channels",
        "write:notification_channels",
        "read:webhooks",
    }
)

# 开放 API 支持订阅的事件类型，与内部事件总线（core.event_bus）发码点保持一致。
VALID_WEBHOOK_EVENTS: frozenset[str] = frozenset(
    {
        "alert.firing",
        "alert.resolved",
        "incident.created",
        "incident.resolved",
        "node.online",
        "node.offline",
    }
)


def dumps_list(values: Optional[list[str]]) -> str:
    """将字符串列表序列化为 JSON 字符串（去重、排序，便于稳定比对）。"""
    if not values:
        return "[]"
    cleaned = sorted({str(v) for v in values if isinstance(v, str) and v})
    return json.dumps(cleaned, ensure_ascii=False)


def loads_list(raw: Optional[str]) -> list[str]:
    """将 JSON 字符串反序列化为字符串列表；解析失败时返回空列表。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if isinstance(x, str)]


class ApiKey(SQLModel, table=True):
    """开放 API 密钥表。

    ``key_hash`` 为 ``sha256(明文)`` 的十六进制串；``prefix`` 取明文密钥体的
    前 8 位，仅用于在认证阶段快速缩小候选集（前缀碰撞概率极低，命中后再做
    常量时间哈希比对）。任何列表/详情接口只回显 ``prefix``，不回显哈希或明文。
    """

    __tablename__ = "api_keys"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=128, description="密钥名称，便于识别")
    key_hash: str = Field(index=True, description="sha256(明文) 十六进制，永不回显")
    prefix: str = Field(
        max_length=16,
        index=True,
        description="明文密钥体前 8 位，用于快速定位候选；非敏感",
    )
    user_id: int = Field(index=True, description="绑定用户 ID，继承其 RBAC 角色")
    scopes: str = Field(
        default="[]",
        description="JSON 数组字符串，授权范围（见 VALID_API_SCOPES）",
    )
    rate_limit: int = Field(
        default=0,
        description="每分钟调用上限；<=0 时按 License edition 默认值",
    )
    last_used_at: Optional[datetime] = Field(
        default=None,
        description="最近一次成功使用时间（节流写入）",
    )
    expires_at: Optional[datetime] = Field(default=None, description="过期时间，NULL=永不过期")
    enabled: bool = Field(default=True, description="是否启用；吊销后置 False")
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")

    # --- helper：读写 scopes ---
    def scopes_list(self) -> list[str]:
        """返回解析后的 scope 列表。"""
        return loads_list(self.scopes)


class WebhookSubscription(SQLModel, table=True):
    """开放 API Webhook 订阅表。

    ``secret`` 用于对出站回调做 HMAC-SHA256 签名；仅在创建接口回显一次，
    列表/详情接口不回显（视为与 API Key 明文同等敏感）。
    """

    __tablename__ = "webhook_subscriptions"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, description="创建/所有者用户 ID")
    url: str = Field(max_length=1024, description="回调接收 URL（https 推荐）")
    secret: str = Field(description="HMAC 签名密钥，仅创建时回显")
    events: str = Field(
        default="[]",
        description="JSON 数组字符串，订阅的事件类型（见 VALID_WEBHOOK_EVENTS）",
    )
    enabled: bool = Field(default=True, description="是否启用")
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
    last_triggered_at: Optional[datetime] = Field(
        default=None,
        description="最近一次成功投递时间",
    )

    def events_list(self) -> list[str]:
        """返回解析后的订阅事件列表。"""
        return loads_list(self.events)
