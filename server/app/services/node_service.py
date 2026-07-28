"""节点管理业务逻辑。"""

import json
import platform as sys_platform
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.agent_auth import create_node_with_tokens, reset_agent_token, revoke_agent_token
from app.core.config import settings
from app.core.event_bus import publish
from app.core.licensing import check_max_nodes_allowed
from app.core.logger import get_logger
from app.models.node import Node

logger = get_logger(__name__)

# 保留名称，禁止普通节点使用
RESERVED_NODE_NAMES = {"__local__"}
VALID_PLATFORMS = {"linux", "windows"}

# 分组名/标签字符限制
GROUP_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
LABEL_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\./@]+$")
LABEL_VALUE_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\./:@]+$")
LABEL_MAX_LENGTH = 128


def _serialize_labels(labels: Optional[dict]) -> Optional[str]:
    """将字典标签序列化为 JSON 字符串；None 则返回 None。"""
    if labels is None:
        return None
    return json.dumps(labels, ensure_ascii=False, sort_keys=True)


def _parse_labels(labels_str: Optional[str]) -> Optional[dict]:
    """将 JSON 字符串标签解析为字典；None 则返回 None。"""
    if labels_str is None:
        return None
    return json.loads(labels_str)


def validate_node_name(name: str) -> None:
    """校验节点名称是否合法。

    Raises:
        ValueError: 名称不合法时抛出，附带原因。
    """
    if not name or len(name) < 1 or len(name) > 64:
        raise ValueError("节点名称长度必须在 1-64 字符之间")
    if name in RESERVED_NODE_NAMES:
        raise ValueError(f"节点名称 '{name}' 为保留名称")


def validate_platform(platform: str) -> None:
    """校验平台类型是否合法。"""
    if platform not in VALID_PLATFORMS:
        raise ValueError(f"平台必须是 {VALID_PLATFORMS} 之一")


def validate_group(group: Optional[str]) -> None:
    """校验分组名是否合法。"""
    if group is None:
        return
    if len(group) > 64:
        raise ValueError("分组名长度不能超过 64 字符")
    if not GROUP_PATTERN.match(group):
        raise ValueError("分组名只能包含字母、数字、中划线、下划线和点")


def validate_labels(labels: Optional[dict]) -> None:
    """校验标签键值对是否合法。"""
    if labels is None:
        return
    if not isinstance(labels, dict):
        raise ValueError("labels 必须是字典")
    for key, value in labels.items():
        if not isinstance(key, str):
            raise ValueError("标签 key 必须是字符串")
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError("标签 value 必须是标量类型")
        str_value = str(value)
        if len(key) > LABEL_MAX_LENGTH:
            raise ValueError(f"标签 key '{key}' 超过最大长度 {LABEL_MAX_LENGTH}")
        if len(str_value) > LABEL_MAX_LENGTH:
            raise ValueError(f"标签 value '{value}' 超过最大长度 {LABEL_MAX_LENGTH}")
        if not LABEL_KEY_PATTERN.match(key):
            raise ValueError(f"标签 key '{key}' 包含非法字符")
        if not LABEL_VALUE_PATTERN.match(str_value):
            raise ValueError(f"标签 value '{value}' 包含非法字符")


def _count_active_nodes(db: Session) -> int:
    """统计计入 License 上限的活跃节点数（未软删除且非本地节点）。

    使用 ``func.count`` 单次聚合查询，避免 ``len(.all())`` 拉取全表。
    """
    return db.exec(
        select(func.count(Node.id)).where(
            Node.is_deleted == False,  # noqa: E712
            Node.is_local == False,  # noqa: E712
        )
    ).one() or 0


def create_node(
    db: Session,
    name: str,
    host: Optional[str] = None,
    description: Optional[str] = None,
    platform: str = "linux",
    labels: Optional[dict] = None,
    group: Optional[str] = None,
    precounted_active: Optional[int] = None,
) -> Node:
    """创建新节点并自动生成认证 Token。

    Args:
        precounted_active: 调用方已预先统计的活跃节点数（批量场景传入，避免每轮
            重复全表 count 导致 O(n²)）；为 None 时本函数自行统计。
    """
    validate_node_name(name)
    validate_platform(platform)
    validate_group(group)
    validate_labels(labels)

    existing = db.exec(select(Node).where(Node.name == name, Node.is_deleted == False)).first()  # noqa: E712
    if existing is not None:
        raise ValueError("节点名称已存在")

    # License 节点数上限检查（本地节点不纳入计数）
    current_count = (
        precounted_active if precounted_active is not None else _count_active_nodes(db)
    )
    if not check_max_nodes_allowed(current_count):
        raise ValueError(
            "当前 License 节点数已达上限，请升级 License 后继续使用"
        )

    if host is not None:
        host_existing = db.exec(
            select(Node).where(
                Node.host == host,
                Node.is_deleted == False,  # noqa: E712
            )
        ).first()
        if host_existing is not None:
            raise ValueError(f"主机地址 '{host}' 已存在")

    labels_str = _serialize_labels(labels)
    return create_node_with_tokens(
        db,
        name=name,
        platform=platform,
        host=host,
        description=description,
        labels=labels_str,
        group=group,
    )


def _node_matches_label(node: Node, label_key: str, label_value: Optional[str]) -> bool:
    """判断节点标签是否匹配筛选条件（label_value 为 None 时只要求 key 存在）。"""
    labels = _parse_labels(node.labels)
    if labels is None:
        return False
    if label_key not in labels:
        return False
    if label_value is not None and str(labels[label_key]) != label_value:
        return False
    return True


def list_nodes(
    db: Session,
    group: Optional[str] = None,
    label_key: Optional[str] = None,
    label_value: Optional[str] = None,
) -> list[Node]:
    """列出所有未软删除的节点，支持按分组和标签筛选。"""
    nodes = db.exec(
        select(Node)
        .where(Node.is_deleted == False)  # noqa: E712
        .order_by(Node.created_at, Node.id)
    ).all()

    if group is not None:
        nodes = [n for n in nodes if n.group == group]

    if label_key is not None:
        nodes = [n for n in nodes if _node_matches_label(n, label_key, label_value)]

    return nodes


def list_nodes_paged(
    db: Session,
    *,
    group: Optional[str] = None,
    label_key: Optional[str] = None,
    label_value: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Node], int]:
    """Web 节点列表分页查询（与开放 API 同为 DB 层真分页口径）。

    返回 (当前页节点, 过滤后总数)。排序为 created_at + id 双关键字：
    id 作 tiebreaker 保证 created_at 相同（如同批导入）时翻页不重复、不遗漏。

    过滤下推取舍：
    - ``group`` 是普通列，直接下推 SQL WHERE，两种后端（SQLite/PG）都走索引友好查询。
    - 标签存在 TEXT(JSON) 列中，下推需要 SQLite ``json_extract`` / PG ``::jsonb ->>``
      两套分歧写法，且对历史非法 JSON 敏感；因此标签过滤保留 Python 端，但**先过滤
      再分页**——在过滤后的完整结果集上切片，分页语义与 DB 分页完全一致。
    """
    filters = [Node.is_deleted == False]  # noqa: E712
    if group is not None:
        filters.append(Node.group == group)

    start = (page - 1) * page_size

    if label_key is not None:
        # 标签过滤无法安全下推双后端 SQL：取过滤后全量（稳定排序）再切片。
        rows = db.exec(
            select(Node).where(*filters).order_by(Node.created_at, Node.id)
        ).all()
        matched = [n for n in rows if _node_matches_label(n, label_key, label_value)]
        return matched[start : start + page_size], len(matched)

    total = db.exec(select(func.count(Node.id)).where(*filters)).one() or 0
    rows = db.exec(
        select(Node)
        .where(*filters)
        .order_by(Node.created_at, Node.id)
        .offset(start)
        .limit(page_size)
    ).all()
    return list(rows), total


def get_node_by_id(db: Session, node_id_value: int) -> Optional[Node]:
    """通过数据库主键 ID 获取未软删除的节点。"""
    node = db.get(Node, node_id_value)
    if node is None or node.is_deleted:
        return None
    return node


def update_node(
    db: Session,
    node: Node,
    name: Optional[str] = None,
    host: Optional[str] = None,
    description: Optional[str] = None,
    platform: Optional[str] = None,
    labels: Optional[dict] = None,
    group: Optional[str] = None,
) -> Node:
    """更新节点业务信息（不修改认证字段）。"""
    if name is not None and name != node.name:
        validate_node_name(name)
        existing = db.exec(
            select(Node).where(
                Node.name == name,
                Node.is_deleted == False,  # noqa: E712
                Node.id != node.id,
            )
        ).first()
        if existing is not None:
            raise ValueError("节点名称已存在")
        node.name = name

    if platform is not None:
        validate_platform(platform)
        node.platform = platform

    if group is not None:
        validate_group(group)
        node.group = group

    if host is not None:
        if host != node.host:
            host_existing = db.exec(
                select(Node).where(
                    Node.host == host,
                    Node.is_deleted == False,  # noqa: E712
                    Node.id != node.id,
                )
            ).first()
            if host_existing is not None:
                raise ValueError(f"主机地址 '{host}' 已存在")
        node.host = host

    if description is not None:
        node.description = description

    if labels is not None:
        validate_labels(labels)
        node.labels = _serialize_labels(labels)

    node.updated_at = datetime.now(timezone.utc)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def delete_node(db: Session, node: Node) -> None:
    """软删除节点（本地节点禁止删除）。"""
    if node.is_local:
        raise ValueError("本地默认节点不允许删除")

    node.is_deleted = True
    node.updated_at = datetime.now(timezone.utc)
    db.add(node)
    db.commit()


def reset_node_token_service(db: Session, node: Node) -> Node:
    """重置节点的 Agent Token 和 Install Key。"""
    if node.is_local:
        raise ValueError("本地默认节点不允许重置 Token")
    return reset_agent_token(db, node)


def revoke_node_token_service(db: Session, node: Node) -> Node:
    """撤销节点的 Agent Token（不生成新 Token）。"""
    if node.is_local:
        raise ValueError("本地默认节点不允许撤销 Token")
    return revoke_agent_token(db, node)


def regenerate_install_key_service(db: Session, node: Node) -> Node:
    """重新生成 Install Key（不修改 Agent Token）。"""
    from app.core.agent_auth import _generate_unique_value, generate_install_key

    node.install_key = _generate_unique_value(db, generate_install_key, Node.install_key)
    node.install_key_expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.INSTALL_KEY_TTL_SECONDS)
    node.install_key_used = False
    node.install_key_used_at = None
    node.updated_at = datetime.now(timezone.utc)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def generate_install_commands(node: Node, server_url: str) -> dict:
    """生成 Linux/Windows 一键安装命令。"""
    server_url = server_url.rstrip("/")
    # 变量赋值放在 sudo 之后：sudo 的 env_reset 会清空管道前设置的环境变量，
    # 而 `sudo VAR=value cmd` 形式由 sudo 显式保留，保证脚本拿得到 SERVER_URL/INSTALL_KEY。
    linux_command = (
        f"curl -fsSL {server_url}/api/v1/agents/install.sh | "
        f"sudo SERVER_URL={server_url} INSTALL_KEY={node.install_key} bash"
    )
    windows_command = (
        f'powershell -Command "Invoke-WebRequest -Uri {server_url}/api/v1/agents/install.bat '
        f'-OutFile install.bat" && install.bat "{server_url}" "{node.install_key}"'
    )
    return {
        "linux_command": linux_command,
        "windows_command": windows_command,
        "install_key_expires_at": node.install_key_expires_at.isoformat(),
    }


def touch_node_online(db: Session, node: Node, now: Optional[datetime] = None) -> bool:
    """更新节点最后在线时间；如果状态从非 online 切换为 online，发布 node.online 事件。

    Returns:
        True 表示状态发生了 online 切换。
    """
    now = now or datetime.now(timezone.utc)
    was_online = node.status == "online"
    node.last_seen = now
    node.status = "online"
    node.updated_at = now
    db.add(node)
    db.commit()
    db.refresh(node)

    if not was_online:
        try:
            publish(
                "node.online",
                {
                    "node_id": node.node_id,
                    "name": node.name,
                    "status": "online",
                    "timestamp": now.isoformat().replace("+00:00", "Z"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"发布 node.online 事件失败: {exc}")

        # 恢复连接通知：断线恢复后补发一条提醒（不丢记录、不轰炸迟到告警）
        try:
            from app.services.notification import send_notification

            send_notification(
                db,
                event_type="node.recovery",
                event_id=node.node_id,
                payload={
                    "title": f"[恢复] 节点 {node.name} 已恢复连接",
                    "message": (
                        f"节点 **{node.name}** 已恢复连接。\n\n"
                        "断线期间的数据已补发，请检查是否有异常。"
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"发送节点恢复通知失败: {exc}")
        return True
    return False


def mark_node_offline(db: Session, node: Node, now: Optional[datetime] = None) -> bool:
    """将节点标记为离线；如果状态从 online 切换，发布 node.offline 事件。

    Returns:
        True 表示状态发生了 offline 切换。
    """
    now = now or datetime.now(timezone.utc)
    was_online = node.status == "online"
    if not was_online:
        return False

    node.status = "offline"
    node.updated_at = now
    db.add(node)
    db.commit()
    db.refresh(node)

    try:
        publish(
            "node.offline",
            {
                "node_id": node.node_id,
                "name": node.name,
                "status": "offline",
                "timestamp": now.isoformat().replace("+00:00", "Z"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"发布 node.offline 事件失败: {exc}")
    return True


def check_and_mark_offline_nodes(db: Session, threshold_seconds: int = 300) -> int:
    """检查并标记超过阈值未上报的节点为离线。

    Returns:
        被标记为离线的节点数量。
    """
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(seconds=threshold_seconds)
    nodes = db.exec(
        select(Node).where(
            Node.status == "online",
            Node.is_deleted == False,  # noqa: E712
        )
    ).all()

    marked = 0
    for node in nodes:
        last_seen = node.last_seen
        # SQLite 可能返回 naive datetime
        if last_seen is not None and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if last_seen is None or last_seen < threshold:
            if mark_node_offline(db, node, now):
                marked += 1
    return marked


def ensure_local_node(db: Session) -> Node:
    """确保本地默认节点存在；不存在则创建。

    Server 启动时调用，用于监控 Server 自身。
    """
    local = db.exec(
        select(Node).where(
            Node.name == "__local__",
            Node.is_deleted == False,  # noqa: E712
        )
    ).first()
    if local is not None:
        return local

    current_platform = "windows" if sys_platform.system().lower() == "windows" else "linux"
    try:
        node = create_node_with_tokens(
            db,
            name="__local__",
            host="localhost",
            description="本地 Agent，用于监控 Server 自身",
            platform=current_platform,
            is_local=True,
            status="online",
        )
    except IntegrityError:
        # 多 worker 并发启动时会同时 INSERT __local__，UNIQUE(nodes.name) 拦截后到者。
        # rollback 必须在重查之前：异常后的 session 不重置，后续查询会直接挂。
        db.rollback()
        local = db.exec(
            select(Node).where(
                Node.name == "__local__",
                Node.is_deleted == False,  # noqa: E712
            )
        ).first()
        if local is None:
            # 重查仍为空：不是并发冲突而是别的约束问题，原样抛出
            raise
        return local
    # 显式设置 last_seen 并发布上线事件
    touch_node_online(db, node)
    return node
