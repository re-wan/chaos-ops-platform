"""事件（Incident）业务逻辑。

负责事件的自动创建、告警聚合、状态流转、合并与手动管理。
"""

import json
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from app.core.utils import now_utc, ensure_utc
from app.core.event_bus import publish
from app.core.incident_event_bus import record_incident_event
from app.core.logger import get_logger
from app.models.alert_event import AlertEvent
from app.services import notification as notification_service
from app.models.alert_rule import AlertRule
from app.models.incident import Incident
from app.models.incident_alert import IncidentAlert
from app.models.node import Node

# 防御式 import：三版物理分包的免费版会删除 ai_analysis_queue.py（AI 自动分析入队），
# 文件存在时（专业/企业版/dev）行为不变，缺失时跳过自动分析入队。
try:
    from app.services import ai_analysis_queue

    HAS_AI_ANALYSIS_QUEUE = True
except ImportError:
    ai_analysis_queue = None  # type: ignore[assignment]
    HAS_AI_ANALYSIS_QUEUE = False

logger = get_logger("services.incident")


def _maybe_enqueue_ai_analysis(session: Session, incident_id: int) -> None:
    """事件创建后尝试入队 AI 根因分析（异常隔离，不影响事件主流程）。"""
    if not HAS_AI_ANALYSIS_QUEUE:
        return
    try:
        ai_analysis_queue.maybe_enqueue_analysis(session, incident_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"AI 分析自动入队失败: incident_id={incident_id}, error={exc}")

VALID_STATUSES = {"open", "acknowledged", "resolved", "closed"}
VALID_SEVERITIES = {"critical", "warning", "info"}


def _publish_incident_event(event_type: str, incident: Incident, extra: Optional[dict] = None) -> None:
    """发布 Incident 实时事件（异常隔离）。"""
    payload = {
        "incident_id": incident.id,
        "title": incident.title,
        "status": incident.status,
        "severity": incident.severity,
        "rule_id": incident.rule_id,
        "node_id": incident.node_id,
    }
    if extra:
        payload.update(extra)
    try:
        publish(event_type, payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"发布 {event_type} 事件失败: {exc}")


def _get_node_name(session: Session, node_id: str) -> Optional[str]:
    """通过业务 node_id 获取节点名称；不存在时返回 None。"""
    node = session.exec(
        select(Node).where(Node.node_id == node_id, Node.is_deleted == False)  # noqa: E712
    ).first()
    if node is None:
        return None
    return node.name


def find_active_incident_for_alert(
    session: Session,
    rule_id: int,
    node_id: str,
) -> Optional[Incident]:
    """查找指定 (rule_id, node_id) 下处于 open/acknowledged 状态的事件。"""
    return session.exec(
        select(Incident).where(
            Incident.rule_id == rule_id,
            Incident.node_id == node_id,
            Incident.status.in_({"open", "acknowledged"}),
            Incident.is_deleted == False,  # noqa: E712
        )
    ).first()


def create_alert_event(
    session: Session,
    rule_id: int,
    node_id: str,
    severity: str,
    message: Optional[str] = None,
    labels: Optional[dict | str] = None,
    fired_at: Optional[datetime] = None,
) -> AlertEvent:
    """创建一条 AlertEvent 记录。"""
    labels_str: Optional[str] = None
    if labels is not None:
        if isinstance(labels, dict):
            labels_str = json.dumps(labels, ensure_ascii=False, sort_keys=True)
        elif isinstance(labels, str):
            labels_str = labels

    event = AlertEvent(
        rule_id=rule_id,
        node_id=node_id,
        severity=severity,
        message=message,
        labels=labels_str,
        fired_at=fired_at or now_utc(),
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def _link_alert_event(
    session: Session,
    incident: Incident,
    alert_event: AlertEvent,
    commit: bool = True,
) -> IncidentAlert:
    """将 AlertEvent 关联到 Incident，避免重复关联。"""
    existing = session.exec(
        select(IncidentAlert).where(
            IncidentAlert.incident_id == incident.id,
            IncidentAlert.alert_event_id == alert_event.id,
        )
    ).first()
    if existing is not None:
        return existing

    link = IncidentAlert(
        incident_id=incident.id,
        alert_event_id=alert_event.id,
    )
    session.add(link)
    if commit:
        session.commit()
    return link


def auto_create_or_update_incident(
    session: Session,
    alert_event: AlertEvent,
) -> Incident:
    """根据 AlertEvent 自动创建 Incident 或聚合到已有 Incident。

    聚合维度：相同 (rule_id, node_id) 且 1 小时窗口内的 open/acknowledged Incident。
    """
    fired_at = ensure_utc(alert_event.fired_at) or now_utc()
    one_hour_ago = fired_at - timedelta(hours=1)

    incident = session.exec(
        select(Incident).where(
            Incident.rule_id == alert_event.rule_id,
            Incident.node_id == alert_event.node_id,
            Incident.status.in_({"open", "acknowledged"}),
            Incident.started_at >= one_hour_ago,
            Incident.is_deleted == False,  # noqa: E712
        )
    ).first()

    if incident is not None:
        _link_alert_event(session, incident, alert_event)
        logger.info(
            f"告警事件聚合到已有 Incident: incident_id={incident.id}, "
            f"alert_event_id={alert_event.id}"
        )
        return incident

    rule = session.get(AlertRule, alert_event.rule_id)
    rule_name = rule.name if rule is not None else f"rule:{alert_event.rule_id}"
    node_name = _get_node_name(session, alert_event.node_id) or alert_event.node_id

    incident = Incident(
        title=f"{rule_name} - {node_name}",
        severity=alert_event.severity,
        status="open",
        source="auto",
        rule_id=alert_event.rule_id,
        node_id=alert_event.node_id,
        started_at=fired_at,
    )
    session.add(incident)
    session.commit()
    session.refresh(incident)

    _link_alert_event(session, incident, alert_event)
    logger.info(
        f"自动创建 Incident: incident_id={incident.id}, "
        f"rule_id={alert_event.rule_id}, node_id={alert_event.node_id}"
    )

    _publish_incident_event("incident.created", incident)

    record_incident_event(
        session=session,
        incident_id=incident.id,
        event_type="system",
        event_subtype="incident.created",
        title="timeline.incident.created.auto.title",
        description="timeline.incident.created.auto.desc",
        metadata={
            "rule_id": alert_event.rule_id,
            "node_id": alert_event.node_id,
            "severity": alert_event.severity,
            "alert_event_id": alert_event.id,
            "incident_title": incident.title,
        },
        source="detector",
        timestamp=incident.created_at,
    )

    # 触发 Incident 创建通知（异步，异常隔离）
    try:
        notification_service.send_incident_notification(
            session=session,
            incident_id=incident.id,
            incident_title=incident.title,
            subtype="incident.created",
            severity=incident.severity,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"触发事件通知失败: incident_id={incident.id}, error={exc}")

    _maybe_enqueue_ai_analysis(session, incident.id)

    return incident


def auto_resolve_incident(
    session: Session,
    rule_id: int,
    node_id: str,
    now: datetime,
) -> Optional[Incident]:
    """告警恢复时，自动 resolve 关联的 open/acknowledged Incident。"""
    incident = session.exec(
        select(Incident).where(
            Incident.rule_id == rule_id,
            Incident.node_id == node_id,
            Incident.status.in_({"open", "acknowledged"}),
            Incident.is_deleted == False,  # noqa: E712
        )
    ).first()

    if incident is not None:
        return resolve_incident(session, incident)
    return None


def create_manual_incident(
    session: Session,
    title: str,
    severity: str,
    description: Optional[str] = None,
    assigned_to: Optional[int] = None,
    alert_event_ids: Optional[list[int]] = None,
    created_by: Optional[int] = None,
) -> Incident:
    """手动创建事件，并可关联已有 AlertEvent。"""
    alert_event_ids = alert_event_ids or []

    incident = Incident(
        title=title,
        description=description,
        severity=severity,
        status="open",
        source="manual",
        created_by=created_by,
        assigned_to=assigned_to,
        started_at=now_utc(),
    )
    session.add(incident)
    session.commit()
    session.refresh(incident)

    for event_id in alert_event_ids:
        event = session.get(AlertEvent, event_id)
        if event is not None:
            _link_alert_event(session, incident, event, commit=False)
    session.commit()

    record_incident_event(
        session=session,
        incident_id=incident.id,
        event_type="system",
        event_subtype="incident.created",
        title="timeline.incident.created.manual.title",
        description="timeline.incident.created.manual.desc",
        metadata={
            "created_by": created_by,
            "severity": incident.severity,
            "incident_title": incident.title,
        },
        source="user",
        created_by=created_by,
        timestamp=incident.created_at,
    )
    logger.info(f"手动创建 Incident: incident_id={incident.id}, created_by={created_by}")

    _publish_incident_event("incident.created", incident)

    # 触发 Incident 创建通知（异步，异常隔离）
    try:
        notification_service.send_incident_notification(
            session=session,
            incident_id=incident.id,
            incident_title=incident.title,
            subtype="incident.created",
            severity=incident.severity,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"触发事件通知失败: incident_id={incident.id}, error={exc}")

    _maybe_enqueue_ai_analysis(session, incident.id)

    return incident


def acknowledge_incident(
    session: Session,
    incident: Incident,
    user_id: int,
) -> Incident:
    """认领事件：open -> acknowledged。"""
    if incident.status == "closed":
        raise ValueError("事件已关闭，无法认领")
    if incident.status != "open":
        raise ValueError("只有 open 状态的事件可以认领")

    incident.status = "acknowledged"
    incident.assigned_to = user_id
    incident.acknowledged_at = now_utc()
    incident.updated_at = now_utc()
    session.add(incident)
    session.commit()
    session.refresh(incident)

    record_incident_event(
        session=session,
        incident_id=incident.id,
        event_type="system",
        event_subtype="incident.acknowledged",
        title="timeline.incident.acknowledged.title",
        description="timeline.incident.acknowledged.desc",
        metadata={"assigned_to": incident.assigned_to, "user_id": user_id},
        source="user",
        created_by=user_id,
        timestamp=incident.acknowledged_at,
    )

    _publish_incident_event("incident.acknowledged", incident, {"acknowledged_by": user_id})
    return incident


def resolve_incident(
    session: Session,
    incident: Incident,
    user_id: Optional[int] = None,
) -> Incident:
    """标记事件已解决：open/acknowledged -> resolved。"""
    if incident.status == "closed":
        raise ValueError("事件已关闭，无法标记解决")
    if incident.status not in {"open", "acknowledged"}:
        raise ValueError("只有 open 或 acknowledged 状态的事件可以标记解决")

    resolved_at = now_utc()
    incident.status = "resolved"
    incident.resolved_at = resolved_at
    if user_id is not None:
        incident.assigned_to = user_id
    incident.updated_at = resolved_at
    session.add(incident)
    session.commit()
    session.refresh(incident)

    record_incident_event(
        session=session,
        incident_id=incident.id,
        event_type="system",
        event_subtype="incident.resolved",
        title=(
            "timeline.incident.resolved.manual.title"
            if user_id is not None
            else "timeline.incident.resolved.auto.title"
        ),
        description=(
            "timeline.incident.resolved.manual.desc"
            if user_id is not None
            else "timeline.incident.resolved.auto.desc"
        ),
        metadata={"assigned_to": incident.assigned_to},
        source="user" if user_id is not None else "detector",
        created_by=user_id,
        timestamp=resolved_at,
    )

    _publish_incident_event("incident.resolved", incident, {"resolved_by": user_id})
    return incident


def close_incident(
    session: Session,
    incident: Incident,
    user_id: Optional[int] = None,
) -> Incident:
    """关闭事件：resolved -> closed。"""
    if incident.status == "closed":
        raise ValueError("事件已关闭")
    if incident.status != "resolved":
        raise ValueError("只有 resolved 状态的事件可以关闭")

    closed_at = now_utc()
    incident.status = "closed"
    incident.closed_at = closed_at
    if user_id is not None:
        incident.assigned_to = user_id
    incident.updated_at = closed_at
    session.add(incident)
    session.commit()
    session.refresh(incident)

    record_incident_event(
        session=session,
        incident_id=incident.id,
        event_type="system",
        event_subtype="incident.closed",
        title=(
            "timeline.incident.closed.manual.title"
            if user_id is not None
            else "timeline.incident.closed.auto.title"
        ),
        description=(
            "timeline.incident.closed.manual.desc"
            if user_id is not None
            else "timeline.incident.closed.auto.desc"
        ),
        source="user" if user_id is not None else "system",
        created_by=user_id,
        timestamp=closed_at,
    )

    _publish_incident_event("incident.closed", incident, {"closed_by": user_id})
    return incident


def merge_incident(
    session: Session,
    source_incident: Incident,
    target_incident: Incident,
) -> Incident:
    """合并事件：将被合并事件关联的告警转移到目标事件，并关闭被合并事件。"""
    if source_incident.id == target_incident.id:
        raise ValueError("不能合并到自身")
    if source_incident.status == "closed":
        raise ValueError("被合并事件已关闭")
    if target_incident.status == "closed":
        raise ValueError("目标事件已关闭")
    if source_incident.is_deleted or target_incident.is_deleted:
        raise ValueError("不能合并已删除的事件")

    links = session.exec(
        select(IncidentAlert).where(IncidentAlert.incident_id == source_incident.id)
    ).all()

    for link in links:
        existing = session.exec(
            select(IncidentAlert).where(
                IncidentAlert.incident_id == target_incident.id,
                IncidentAlert.alert_event_id == link.alert_event_id,
            )
        ).first()
        if existing is None:
            link.incident_id = target_incident.id
            session.add(link)
        else:
            # 目标事件已包含该告警事件，删除源事件侧的重复关联
            session.delete(link)

    source_incident.status = "closed"
    source_incident.closed_at = now_utc()
    source_incident.updated_at = now_utc()
    merge_note = f"已合并到事件 #{target_incident.id}"
    if source_incident.description:
        source_incident.description = source_incident.description + "\n" + merge_note
    else:
        source_incident.description = merge_note
    session.add(source_incident)
    session.commit()
    session.refresh(source_incident)
    session.refresh(target_incident)

    merged_at = source_incident.closed_at or now_utc()
    record_incident_event(
        session=session,
        incident_id=source_incident.id,
        event_type="system",
        event_subtype="incident.merged",
        title="timeline.incident.merged.source.title",
        description="timeline.incident.merged.source.desc",
        metadata={"target_incident_id": target_incident.id},
        source="user",
        timestamp=merged_at,
    )
    record_incident_event(
        session=session,
        incident_id=target_incident.id,
        event_type="system",
        event_subtype="incident.merged",
        title="timeline.incident.merged.target.title",
        description="timeline.incident.merged.target.desc",
        metadata={"source_incident_id": source_incident.id},
        source="user",
        timestamp=merged_at,
    )

    logger.info(
        f"合并 Incident: source={source_incident.id}, target={target_incident.id}"
    )

    _publish_incident_event(
        "incident.merged",
        source_incident,
        {"target_incident_id": target_incident.id},
    )
    _publish_incident_event(
        "incident.merged",
        target_incident,
        {"source_incident_id": source_incident.id},
    )
    return source_incident


def list_incidents(
    session: Session,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    node_id: Optional[str] = None,
) -> list[Incident]:
    """列出事件，支持 status/severity/node_id 过滤。"""
    query = select(Incident).where(Incident.is_deleted == False)  # noqa: E712
    if status is not None:
        query = query.where(Incident.status == status)
    if severity is not None:
        query = query.where(Incident.severity == severity)
    if node_id is not None:
        query = query.where(Incident.node_id == node_id)
    query = query.order_by(Incident.created_at.desc())
    return list(session.exec(query).all())


def get_incident(session: Session, incident_id: int) -> Optional[Incident]:
    """通过 ID 获取事件。"""
    return session.get(Incident, incident_id)


def get_incident_with_alerts(
    session: Session,
    incident_id: int,
) -> tuple[Optional[Incident], list[IncidentAlert]]:
    """获取事件详情及其关联的告警事件列表。"""
    incident = session.get(Incident, incident_id)
    if incident is None:
        return None, []

    alerts = list(
        session.exec(
            select(IncidentAlert)
            .where(IncidentAlert.incident_id == incident.id)
            .order_by(IncidentAlert.added_at.desc())
        ).all()
    )
    return incident, alerts


def update_incident(
    session: Session,
    incident: Incident,
    title: Optional[str] = None,
    description: Optional[str] = None,
    assigned_to: Optional[int] = None,
) -> Incident:
    """更新事件基础信息（标题、描述、负责人）。"""
    if incident.status == "closed":
        raise ValueError("事件已关闭，无法更新")
    if incident.is_deleted:
        raise ValueError("事件已删除")

    if title is not None:
        incident.title = title
    if description is not None:
        incident.description = description
    if assigned_to is not None:
        incident.assigned_to = assigned_to

    incident.updated_at = now_utc()
    session.add(incident)
    session.commit()
    session.refresh(incident)
    return incident


def delete_incident(session: Session, incident: Incident) -> Incident:
    """软删除事件，保留历史。"""
    incident.is_deleted = True
    incident.updated_at = now_utc()
    session.add(incident)
    session.commit()
    session.refresh(incident)
    return incident
