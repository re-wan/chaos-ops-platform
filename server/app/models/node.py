"""Node（被监控节点）数据模型。"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Index, text
from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class Node(SQLModel, table=True):
    """被监控节点表。

    包含节点基础信息、状态、认证字段以及软删除/本地节点标记。
    """

    __tablename__ = "nodes"

    __table_args__ = (
        # 支持按分组+状态快速筛选
        Index("ix_nodes_group_status", "group", "status"),
        # name 仅在未软删除的行中唯一：软删除后允许同名重建。
        # 部分唯一索引（partial unique index）——SQLite 与 PostgreSQL 均支持；
        # 若用全表 UNIQUE(name)，软删除行仍会占位，同名重建会触发 IntegrityError。
        Index(
            "uq_nodes_name_active",
            "name",
            unique=True,
            sqlite_where=text("is_deleted = 0"),
            postgresql_where=text("is_deleted = false"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True, description="数据库主键，内部使用")
    node_id: str = Field(index=True, unique=True, description="业务节点标识，如 node_abc123")
    # 保留普通索引（按名称查询/筛选频繁）；唯一性由 uq_nodes_name_active 部分唯一索引保证
    name: str = Field(index=True, description="节点名称，未软删除节点中全局唯一")
    host: Optional[str] = Field(default=None, description="主机地址或域名")
    description: Optional[str] = Field(default=None, description="节点描述")
    platform: str = Field(default="linux", description="平台：linux / windows")
    group: Optional[str] = Field(
        default=None,
        max_length=64,
        index=True,
        description="节点分组，如 web-server",
    )
    labels: Optional[str] = Field(default=None, description="JSON 格式的标签")
    status: str = Field(default="unknown", description="节点状态")
    last_seen: Optional[datetime] = Field(default=None, description="最后心跳时间")

    # 认证相关字段，详见 docs/auth/AGENT_AUTH_DESIGN.md
    agent_token: str = Field(index=True, unique=True, description="长期 Agent 认证令牌，前缀 a_")
    token_issued_at: datetime = Field(default_factory=now_utc, description="当前 Agent Token 签发时间")
    token_revoked_at: Optional[datetime] = Field(default=None, description="上一批 Agent Token 的撤销时间")
    install_key: str = Field(index=True, unique=True, description="一次性安装密钥，前缀 ik_")
    install_key_expires_at: datetime = Field(description="Install Key 过期时间")
    install_key_used: bool = Field(default=False, description="Install Key 是否已使用")
    install_key_used_at: Optional[datetime] = Field(default=None, description="Install Key 使用时间")

    is_local: bool = Field(default=False, description="是否为本地默认 Agent 节点")
    is_deleted: bool = Field(default=False, description="是否已软删除")
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")

