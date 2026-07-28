"""Agent Token / Install Key 生成与管理工具。"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlmodel import Session, select

from app.core.config import settings
from app.models.node import Node


def generate_agent_token() -> str:
    """生成长期 Agent Token，前缀 a_，长度不少于 64 字节。"""
    return "a_" + secrets.token_urlsafe(64)


def generate_install_key() -> str:
    """生成一次性 Install Key，前缀 ik_，长度不少于 64 字节。"""
    return "ik_" + secrets.token_urlsafe(64)


def generate_node_id() -> str:
    """生成业务节点标识，如 node_abc123。"""
    return "node_" + secrets.token_hex(8)


def _generate_unique_value(
    db: Session,
    generator: Callable[[], str],
    field,
) -> str:
    """生成数据库唯一的随机字符串，冲突时最多重试 3 次。"""
    for _ in range(3):
        value = generator()
        existing = db.exec(select(Node).where(field == value)).first()
        if existing is None:
            return value
    raise RuntimeError("无法生成唯一的节点标识/Token")


def create_node_with_tokens(
    db: Session,
    name: str,
    platform: str = "linux",
    host: Optional[str] = None,
    description: Optional[str] = None,
    labels: Optional[str] = None,
    group: Optional[str] = None,
    is_local: bool = False,
    status: str = "unknown",
) -> Node:
    """创建节点并自动生成 agent_token、install_key 和 node_id。

    Args:
        db: 数据库 Session
        name: 节点名称
        platform: 平台 linux/windows
        host: 主机地址
        description: 节点描述
        labels: JSON 格式标签字符串
        group: 节点分组
        is_local: 是否为本地默认节点

    Returns:
        创建完成的 Node 对象
    """
    ttl_seconds = settings.INSTALL_KEY_TTL_SECONDS
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    now = datetime.now(timezone.utc)

    node = Node(
        node_id=_generate_unique_value(db, generate_node_id, Node.node_id),
        name=name,
        platform=platform,
        host=host,
        description=description,
        group=group,
        labels=labels,
        agent_token=_generate_unique_value(db, generate_agent_token, Node.agent_token),
        token_issued_at=now,
        install_key=_generate_unique_value(db, generate_install_key, Node.install_key),
        install_key_expires_at=expires_at,
        is_local=is_local,
        status=status,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def reset_agent_token(db: Session, node: Node) -> Node:
    """重置 Agent Token 和 Install Key（例如 Token 泄露或需要重新部署）。

    重置时会将当前 token_issued_at 写入 token_revoked_at，
    再生成新的 agent_token 与 token_issued_at，使旧 Token 立即失效。

    Args:
        db: 数据库 Session
        node: 目标节点

    Returns:
        更新后的 Node 对象
    """
    now = datetime.now(timezone.utc)
    node.token_revoked_at = node.token_issued_at
    node.agent_token = _generate_unique_value(db, generate_agent_token, Node.agent_token)
    node.token_issued_at = now
    node.install_key = _generate_unique_value(db, generate_install_key, Node.install_key)
    node.install_key_expires_at = now + timedelta(seconds=settings.INSTALL_KEY_TTL_SECONDS)
    node.install_key_used = False
    node.install_key_used_at = None
    node.updated_at = now
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def revoke_agent_token(db: Session, node: Node) -> Node:
    """仅撤销当前 Agent Token，不生成新 Token。

    用于 Token 泄露后紧急失效，节点需要重新 reset 才能恢复上报。

    Args:
        db: 数据库 Session
        node: 目标节点

    Returns:
        更新后的 Node 对象
    """
    now = datetime.now(timezone.utc)
    node.token_revoked_at = node.token_issued_at
    node.updated_at = now
    db.add(node)
    db.commit()
    db.refresh(node)
    return node
