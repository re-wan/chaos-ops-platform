"""用户操作审计日志服务。

复用事件时间线（incident_event_bus）写入 event_type="user" 的持久化记录，
将用户管理相关操作（创建、修改、删除）沉淀为审计线索。
"""

from typing import Optional

from sqlmodel import Session

from app.core.incident_event_bus import record_incident_event
from app.models.alert_rule import AlertRule
from app.models.notification_channel import NotificationChannel
from app.models.user import User

# 用户审计事件使用统一的 incident_id 占位符，因为当前时间线表以 incident_id 为索引，
# 但用户操作并不关联具体 Incident。该表未设置外键约束，使用 0 作为系统级审计桶。
SYSTEM_AUDIT_INCIDENT_ID = 0


def _record_user_event(
    session: Session,
    subtype: str,
    title: str,
    description: Optional[str],
    metadata: Optional[dict],
    created_by: Optional[int],
) -> None:
    """写入一条 user 类型的时间线事件，异常被隔离，不抛给调用方。"""
    record_incident_event(
        session=session,
        incident_id=SYSTEM_AUDIT_INCIDENT_ID,
        event_type="user",
        event_subtype=subtype,
        title=title,
        description=description,
        metadata=metadata,
        source="user",
        created_by=created_by,
    )


def record_user_created(
    session: Session,
    user: User,
    created_by: Optional[int] = None,
) -> None:
    """记录用户创建操作。"""
    _record_user_event(
        session=session,
        subtype="user.created",
        title=f"创建用户 {user.username}",
        description=None,
        metadata={
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
            "is_active": user.is_active,
        },
        created_by=created_by,
    )


def record_user_updated(
    session: Session,
    user: User,
    changes: dict,
    created_by: Optional[int] = None,
) -> None:
    """记录用户更新操作。

    Args:
        changes: 发生变更的字段字典，例如 {"role": ("viewer", "admin")}。
    """
    _record_user_event(
        session=session,
        subtype="user.updated",
        title=f"更新用户 {user.username}",
        description=None,
        metadata={
            "user_id": user.id,
            "username": user.username,
            "changes": changes,
        },
        created_by=created_by,
    )


def record_user_deleted(
    session: Session,
    user: User,
    created_by: Optional[int] = None,
) -> None:
    """记录用户删除操作。"""
    _record_user_event(
        session=session,
        subtype="user.deleted",
        title=f"删除用户 {user.username}",
        description=None,
        metadata={
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
        },
        created_by=created_by,
    )


def record_password_reset(
    session: Session,
    user: User,
    created_by: Optional[int] = None,
) -> None:
    """记录密码重置操作（不记录新密码）。"""
    _record_user_event(
        session=session,
        subtype="user.password_reset",
        title=f"重置用户 {user.username} 的密码",
        description=None,
        metadata={
            "user_id": user.id,
            "username": user.username,
        },
        created_by=created_by,
    )


def record_backup_created(
    session: Session,
    filename: str,
    created_by: Optional[int] = None,
) -> None:
    """记录手动/定时备份创建操作。"""
    _record_user_event(
        session=session,
        subtype="backup.created",
        title=f"创建数据库备份 {filename}",
        description=None,
        metadata={"filename": filename},
        created_by=created_by,
    )


def record_backup_restored(
    session: Session,
    filename: str,
    created_by: Optional[int] = None,
) -> None:
    """记录数据库恢复操作。"""
    _record_user_event(
        session=session,
        subtype="backup.restored",
        title=f"从备份 {filename} 恢复数据库",
        description=None,
        metadata={"filename": filename},
        created_by=created_by,
    )


def record_backup_deleted(
    session: Session,
    filename: str,
    created_by: Optional[int] = None,
) -> None:
    """记录备份删除操作。"""
    _record_user_event(
        session=session,
        subtype="backup.deleted",
        title=f"删除数据库备份 {filename}",
        description=None,
        metadata={"filename": filename},
        created_by=created_by,
    )


def record_bulk_nodes_created(
    session: Session,
    task_id: int,
    total_count: int,
    success_count: int,
    failure_count: int,
    created_by: Optional[int] = None,
) -> None:
    """记录批量创建节点操作。"""
    _record_user_event(
        session=session,
        subtype="node.bulk_created",
        title=f"批量创建节点任务 {task_id}",
        description=None,
        metadata={
            "task_id": task_id,
            "total_count": total_count,
            "success_count": success_count,
            "failure_count": failure_count,
        },
        created_by=created_by,
    )


def record_metric_backend_switched(
    session: Session,
    backend_type: str,
    created_by: Optional[int] = None,
) -> None:
    """记录指标后端切换操作。"""
    _record_user_event(
        session=session,
        subtype="metric_backend.switched",
        title=f"切换指标后端为 {backend_type}",
        description=None,
        metadata={"backend_type": backend_type},
        created_by=created_by,
    )


def record_metric_migration(
    session: Session,
    source: str,
    target: str,
    count: int,
    created_by: Optional[int] = None,
) -> None:
    """记录指标数据迁移操作。"""
    _record_user_event(
        session=session,
        subtype="metric_backend.migration",
        title=f"迁移指标数据 {source} -> {target}",
        description=f"共迁移 {count} 条样本",
        metadata={"source": source, "target": target, "count": count},
        created_by=created_by,
    )


# ---- Web 控制台 CRUD 审计（与开放 API 审计同一口径：who/when/what/目标）----
# 复用 user 类型时间线（系统审计桶 incident_id=0）。写操作成功后才调用；
# 底层 record_incident_event 异常隔离，审计失败记日志、绝不阻断业务。


def record_alert_rule_created(
    session: Session,
    rule: AlertRule,
    created_by: Optional[int] = None,
) -> None:
    """记录告警规则创建（Web 控制台）。"""
    _record_user_event(
        session=session,
        subtype="alert_rule.created",
        title=f"创建告警规则 {rule.name}",
        description=None,
        metadata={
            "rule_id": rule.id,
            "name": rule.name,
            "scope": rule.scope,
            "severity": rule.severity,
            "enabled": rule.enabled,
        },
        created_by=created_by,
    )


def record_alert_rule_updated(
    session: Session,
    rule: AlertRule,
    changed_fields: list[str],
    created_by: Optional[int] = None,
) -> None:
    """记录告警规则更新（Web 控制台）。changed_fields 为本次提交的字段名列表。"""
    _record_user_event(
        session=session,
        subtype="alert_rule.updated",
        title=f"更新告警规则 {rule.name}",
        description=None,
        metadata={
            "rule_id": rule.id,
            "name": rule.name,
            "changed_fields": sorted(changed_fields),
        },
        created_by=created_by,
    )


def record_alert_rule_deleted(
    session: Session,
    rule_id: int,
    name: str,
    created_by: Optional[int] = None,
) -> None:
    """记录告警规则删除（Web 控制台）。rule 已删除，由调用方预取 id/name。"""
    _record_user_event(
        session=session,
        subtype="alert_rule.deleted",
        title=f"删除告警规则 {name}",
        description=None,
        metadata={"rule_id": rule_id, "name": name},
        created_by=created_by,
    )


def record_notification_channel_created(
    session: Session,
    channel: NotificationChannel,
    created_by: Optional[int] = None,
) -> None:
    """记录通知渠道创建（Web 控制台）。不含 config，避免敏感信息落地。"""
    _record_user_event(
        session=session,
        subtype="notification_channel.created",
        title=f"创建通知渠道 {channel.name}",
        description=None,
        metadata={
            "channel_id": channel.id,
            "name": channel.name,
            "channel_type": channel.channel_type,
            "enabled": channel.enabled,
        },
        created_by=created_by,
    )


def record_notification_channel_updated(
    session: Session,
    channel: NotificationChannel,
    changed_fields: list[str],
    created_by: Optional[int] = None,
) -> None:
    """记录通知渠道更新（Web 控制台）。只记字段名，不记 config 等敏感取值。"""
    _record_user_event(
        session=session,
        subtype="notification_channel.updated",
        title=f"更新通知渠道 {channel.name}",
        description=None,
        metadata={
            "channel_id": channel.id,
            "name": channel.name,
            "channel_type": channel.channel_type,
            "changed_fields": sorted(changed_fields),
        },
        created_by=created_by,
    )


def record_notification_channel_deleted(
    session: Session,
    channel_id: int,
    name: str,
    channel_type: str,
    created_by: Optional[int] = None,
) -> None:
    """记录通知渠道删除（Web 控制台）。渠道已删除，由调用方预取标识字段。"""
    _record_user_event(
        session=session,
        subtype="notification_channel.deleted",
        title=f"删除通知渠道 {name}",
        description=None,
        metadata={
            "channel_id": channel_id,
            "name": name,
            "channel_type": channel_type,
        },
        created_by=created_by,
    )


# ---- 开放 API（Phase 3 Step 04）审计 ----
# API Key 的管理操作（admin 通过 Web 控制台发起）沿用 user 类型时间线；
# 每一次开放 API 调用则记为 system 类型（高频、轻量，仅含 key_id/user/path/status）。


def record_api_key_created(
    session: Session,
    key_id: int,
    name: str,
    prefix: str,
    user_id: int,
    scopes: list[str],
    created_by: Optional[int] = None,
) -> None:
    """记录 API Key 创建（不记录明文/哈希，仅 prefix）。"""
    _record_user_event(
        session=session,
        subtype="api_key.created",
        title=f"创建 API Key {name} ({prefix}…)",
        description=None,
        metadata={
            "key_id": key_id,
            "name": name,
            "prefix": prefix,
            "user_id": user_id,
            "scopes": list(scopes),
        },
        created_by=created_by,
    )


def record_api_key_revoked(
    session: Session,
    key_id: int,
    name: str,
    created_by: Optional[int] = None,
) -> None:
    """记录 API Key 吊销。"""
    _record_user_event(
        session=session,
        subtype="api_key.revoked",
        title=f"吊销 API Key {name}",
        description=None,
        metadata={"key_id": key_id, "name": name},
        created_by=created_by,
    )


def record_api_key_enabled(
    session: Session,
    key_id: int,
    name: str,
    created_by: Optional[int] = None,
) -> None:
    """记录 API Key 重新启用。"""
    _record_user_event(
        session=session,
        subtype="api_key.enabled",
        title=f"启用 API Key {name}",
        description=None,
        metadata={"key_id": key_id, "name": name},
        created_by=created_by,
    )


def record_api_key_rotated(
    session: Session,
    key_id: int,
    name: str,
    new_prefix: str,
    created_by: Optional[int] = None,
) -> None:
    """记录 API Key 轮转（不记录新明文/哈希，仅新 prefix）。"""
    _record_user_event(
        session=session,
        subtype="api_key.rotated",
        title=f"轮转 API Key {name}",
        description=None,
        metadata={"key_id": key_id, "name": name, "new_prefix": new_prefix},
        created_by=created_by,
    )


def record_open_api_call(
    session: Session,
    key_id: Optional[int],
    user_id: Optional[int],
    method: str,
    path: str,
    status_code: int,
) -> None:
    """记录一次开放 API 调用（高频、轻量）。

    只记录关键索引字段，不记录请求/响应体，避免敏感数据落地与写放大。
    异常被隔离，不影响主流程。
    """
    record_incident_event(
        session=session,
        incident_id=SYSTEM_AUDIT_INCIDENT_ID,
        event_type="system",
        event_subtype="open_api.call",
        title=f"开放 API {method} {path} -> {status_code}",
        description=None,
        metadata={
            "key_id": key_id,
            "user_id": user_id,
            "method": method,
            "path": path,
            "status_code": status_code,
        },
        source="system",
        created_by=user_id,
    )
