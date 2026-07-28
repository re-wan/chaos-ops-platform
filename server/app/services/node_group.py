"""节点组业务逻辑。

Phase 1 最小实现：仅提供 CRUD 与节点 ID 列表维护。
"""

import json
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.core.logger import get_logger
from app.models.node_group import NodeGroup

logger = get_logger("services.node_group")


def _serialize_node_ids(node_ids: list[str]) -> str:
    """将节点 ID 列表序列化为 JSON 字符串。"""
    return json.dumps(list(node_ids), ensure_ascii=False)


def _parse_node_ids(node_ids_str: Optional[str]) -> list[str]:
    """将 JSON 字符串解析为节点 ID 列表。"""
    if not node_ids_str:
        return []
    try:
        data = json.loads(node_ids_str)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


def list_node_groups(session: Session) -> list[NodeGroup]:
    """列出所有节点组。"""
    return list(session.exec(select(NodeGroup).order_by(NodeGroup.created_at)).all())


def get_node_group_by_id(session: Session, group_id: int) -> Optional[NodeGroup]:
    """根据 ID 获取节点组。"""
    return session.get(NodeGroup, group_id)


def create_node_group(
    session: Session,
    name: str,
    description: Optional[str],
    node_ids: list[str],
) -> NodeGroup:
    """创建节点组。"""
    if not name or len(name) > 128:
        raise ValueError("节点组名称长度必须在 1-128 字符之间")

    existing = session.exec(select(NodeGroup).where(NodeGroup.name == name)).first()
    if existing is not None:
        raise ValueError("节点组名称已存在")

    group = NodeGroup(
        name=name,
        description=description,
        node_ids=_serialize_node_ids(node_ids),
    )
    session.add(group)
    session.commit()
    session.refresh(group)
    logger.info(f"创建节点组: id={group.id}, name={name}")
    return group


def update_node_group(
    session: Session,
    group: NodeGroup,
    name: Optional[str] = None,
    description: Optional[str] = None,
    node_ids: Optional[list[str]] = None,
) -> NodeGroup:
    """更新节点组。"""
    if name is not None and name != group.name:
        if not name or len(name) > 128:
            raise ValueError("节点组名称长度必须在 1-128 字符之间")
        existing = session.exec(
            select(NodeGroup).where(
                NodeGroup.name == name,
                NodeGroup.id != group.id,
            )
        ).first()
        if existing is not None:
            raise ValueError("节点组名称已存在")
        group.name = name

    if description is not None:
        group.description = description
    if node_ids is not None:
        group.node_ids = _serialize_node_ids(node_ids)

    group.updated_at = datetime.now(timezone.utc)
    session.add(group)
    session.commit()
    session.refresh(group)
    logger.info(f"更新节点组: id={group.id}, name={group.name}")
    return group


def delete_node_group(session: Session, group: NodeGroup) -> None:
    """删除节点组。"""
    session.delete(group)
    session.commit()
    logger.info(f"删除节点组: id={group.id}, name={group.name}")
