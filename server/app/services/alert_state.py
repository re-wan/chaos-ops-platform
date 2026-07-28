"""告警规则状态持久化与状态转换。

负责维护 (rule_id, node_id) 粒度的 AlertState，
并在状态变化时驱动事件生命周期的相关动作。
"""

from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.core.utils import ensure_utc
from app.core.event_bus import publish
from app.core.logger import get_logger
from app.models.alert_rule import AlertRule
from app.models.alert_state import AlertState

logger = get_logger("services.alert_state")


def get_or_create_state(
    session: Session,
    rule_id: int,
    node_id: str,
) -> AlertState:
    """获取或创建指定规则/节点的 AlertState。"""
    state = session.exec(
        select(AlertState).where(
            AlertState.rule_id == rule_id,
            AlertState.node_id == node_id,
        )
    ).first()

    if state is None:
        state = AlertState(
            rule_id=rule_id,
            node_id=node_id,
            state="idle",
        )
        session.add(state)
        session.commit()
        session.refresh(state)

    return state


def transition_state(
    session: Session,
    state: AlertState,
    rule: AlertRule,
    condition_met: bool,
    now: datetime,
) -> str:
    """根据当前条件和规则配置转换 AlertState。

    状态转换规则：
        idle + 满足     -> pending
        pending + 满足 N 秒 -> firing
        pending + 不满足 M 秒 -> resolved
        firing + 不满足 M 秒 -> resolved
        resolved/idle + 满足 -> pending

    firing 时的事件创建/聚合在 alert_detector._process_firing_notification 中完成，
    因为那里已经通过了抑制、静默、去重检查。
    resolved 时自动 resolve 关联的 open/acknowledged Incident。

    返回：转换后的状态字符串。
    """
    # 延迟导入，避免循环依赖
    from app.core.incident_event_bus import record_incident_event
    from app.services import incident as incident_service

    pending_duration = timedelta(seconds=rule.pending_duration_seconds)
    resolve_duration = timedelta(seconds=rule.resolve_duration_seconds)

    old_state = state.state
    new_state = old_state

    if condition_met:
        state.last_met_at = now
        if old_state in {"idle", "resolved"}:
            new_state = "pending"
            state.pending_since = now

        # 新进入 pending 或已在 pending：检查是否满足持续时长
        pending_since = ensure_utc(state.pending_since)
        if pending_since is not None and (
            now - pending_since >= pending_duration
        ):
            new_state = "firing"
    else:
        state.last_not_met_at = now
        if old_state in {"pending", "firing"}:
            # 以条件最后一次满足时间为基准计算不满足持续时长
            reference = ensure_utc(state.last_met_at) or ensure_utc(
                state.pending_since
            ) or now
            if now - reference >= resolve_duration:
                new_state = "resolved"
                state.pending_since = None

    if new_state != old_state:
        state.state = new_state
        if new_state == "resolved":
            resolved_incident = None
            try:
                resolved_incident = incident_service.auto_resolve_incident(
                    session, rule.id, state.node_id, now
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    f"告警恢复时自动 resolve Incident 失败: "
                    f"rule_id={rule.id}, node_id={state.node_id}, error={exc}"
                )

            if resolved_incident is not None:
                try:
                    record_incident_event(
                        session=session,
                        incident_id=resolved_incident.id,
                        event_type="alert",
                        event_subtype="alert.resolved",
                        title="timeline.alert.resolved.title",
                        description="timeline.alert.resolved.desc",
                        metadata={
                            "rule_id": rule.id,
                            "rule_name": rule.name,
                            "node_id": state.node_id,
                        },
                        source="detector",
                        timestamp=now,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        f"记录 alert.resolved 时间线事件失败: "
                        f"incident_id={resolved_incident.id}, error={exc}"
                    )

        logger.info(
            f"状态转换: rule_id={rule.id}, node_id={state.node_id}, "
            f"{old_state} -> {new_state}"
        )

    # resolved 后清空上次通知时间，确保恢复后再次 firing 会重新通知
    if new_state == "resolved":
        state.last_notified_at = None

    state.updated_at = now
    session.add(state)
    session.commit()

    # 发布实时事件（异常隔离）
    if old_state != new_state and new_state == "resolved":
        try:
            publish(
                "alert.resolved",
                {
                    "rule_id": rule.id,
                    "rule_name": rule.name,
                    "node_id": state.node_id,
                    "timestamp": now.isoformat().replace("+00:00", "Z"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"发布 alert.resolved 事件失败: {exc}")

    return state.state
