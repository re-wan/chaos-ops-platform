"""告警抑制规则业务逻辑。"""

import json
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.core.logger import get_logger
from app.models.alert_inhibition import AlertInhibition
from app.models.alert_state import AlertState

logger = get_logger("audit.alert_inhibition")


def _parse_json_field(field_str: str) -> dict | list:
    """解析 JSON 字段，失败返回空对象。"""
    try:
        data = json.loads(field_str)
        if isinstance(data, (dict, list)):
            return data
    except json.JSONDecodeError:
        pass
    return {} if field_str.startswith("{") else []


def _labels_match(matchers: dict, labels: dict) -> bool:
    """labels 满足 matchers 中所有键值对时返回 True。"""
    return all(labels.get(k) == v for k, v in matchers.items())


def is_inhibited(
    session: Session,
    rule_id: int,
    node_id: str,
    labels: Optional[dict],
    now: datetime,
    _depth: int = 0,
) -> bool:
    """判断指定告警是否被抑制。

    查找当前 firing 的父告警（source_matchers 匹配），并且父告警与当前告警
    在 equal_labels 指定标签上取值相同，则当前告警被抑制。

    MVP 阶段限制抑制链深度为 1，不支持链式抑制，避免循环抑制。
    """
    if _depth > 0:
        # 不支持链式抑制，避免循环抑制
        return False

    labels = labels or {}
    inhibitions = session.exec(select(AlertInhibition)).all()
    if not inhibitions:
        return False

    # 当前所有 firing 的告警状态
    firing_states = session.exec(
        select(AlertState).where(AlertState.state == "firing")
    ).all()
    if not firing_states:
        return False

    for inhibition in inhibitions:
        source_matchers = _parse_json_field(inhibition.source_matchers)
        target_matchers = _parse_json_field(inhibition.target_matchers)
        equal_labels = _parse_json_field(inhibition.equal_labels)
        if not isinstance(equal_labels, list):
            equal_labels = []

        # 先判断当前告警是否匹配 target
        if not _labels_match(target_matchers, labels):
            continue

        # 查找匹配的 source firing 告警
        for source_state in firing_states:
            # 不抑制自身
            if source_state.rule_id == rule_id and source_state.node_id == node_id:
                continue

            source_rule_labels = _get_rule_labels(session, source_state.rule_id)
            if source_rule_labels is None:
                continue

            if not _labels_match(source_matchers, source_rule_labels):
                continue

            # 检查 equal_labels 是否一致
            if all(
                labels.get(label) == source_rule_labels.get(label)
                for label in equal_labels
            ):
                logger.info(
                    f"告警被抑制: rule_id={rule_id}, node_id={node_id}, "
                    f"labels={labels}, source_rule_id={source_state.rule_id}, "
                    f"inhibition_id={inhibition.id}"
                )
                return True

    return False


def _get_rule_labels(session: Session, rule_id: int) -> Optional[dict]:
    """获取规则的 labels 信息，用于抑制匹配。

    当前从 AlertRule 的 scope_target 与规则 ID 构造标识性 labels。
    Phase 2 可扩展为读取规则关联的标签元数据。
    """
    from app.models.alert_rule import AlertRule

    rule = session.get(AlertRule, rule_id)
    if rule is None or not rule.enabled:
        return None

    labels: dict[str, str] = {
        "rule_id": str(rule.id),
        "severity": rule.severity,
    }
    if rule.scope == "node" and rule.scope_target:
        labels["node_id"] = rule.scope_target
    return labels


def list_inhibitions(session: Session) -> list[AlertInhibition]:
    """列出所有抑制规则。"""
    return list(
        session.exec(
            select(AlertInhibition).order_by(AlertInhibition.created_at)
        ).all()
    )


def create_inhibition(
    session: Session,
    source_matchers: dict,
    target_matchers: dict,
    equal_labels: list[str],
) -> AlertInhibition:
    """创建抑制规则。"""
    if not isinstance(source_matchers, dict):
        raise ValueError("source_matchers 必须是 JSON 对象")
    if not isinstance(target_matchers, dict):
        raise ValueError("target_matchers 必须是 JSON 对象")
    if not isinstance(equal_labels, list) or not all(
        isinstance(x, str) for x in equal_labels
    ):
        raise ValueError("equal_labels 必须是字符串列表")

    inhibition = AlertInhibition(
        source_matchers=json.dumps(source_matchers, sort_keys=True, ensure_ascii=False),
        target_matchers=json.dumps(target_matchers, sort_keys=True, ensure_ascii=False),
        equal_labels=json.dumps(equal_labels, ensure_ascii=False),
    )
    session.add(inhibition)
    session.commit()
    session.refresh(inhibition)

    logger.info(
        f"创建抑制规则: id={inhibition.id}, "
        f"source_matchers={inhibition.source_matchers}, "
        f"target_matchers={inhibition.target_matchers}, "
        f"equal_labels={inhibition.equal_labels}"
    )
    return inhibition


def delete_inhibition(session: Session, inhibition_id: int) -> bool:
    """删除抑制规则。"""
    inhibition = session.get(AlertInhibition, inhibition_id)
    if inhibition is None:
        return False

    session.delete(inhibition)
    session.commit()

    logger.info(f"删除抑制规则: id={inhibition_id}")
    return True
