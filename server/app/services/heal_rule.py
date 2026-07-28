"""自愈规则业务逻辑。"""

import json
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.core.logger import get_logger
from app.models.alert_rule import AlertRule
from app.models.heal_action import HealAction
from app.models.heal_rule import HealRule

logger = get_logger("services.heal_rule")


def get_heal_rule_by_id(session: Session, rule_id: int) -> Optional[HealRule]:
    """根据 ID 获取自愈规则。"""
    return session.get(HealRule, rule_id)


def list_heal_rules(session: Session) -> list[HealRule]:
    """列出所有自愈规则。"""
    return list(
        session.exec(select(HealRule).order_by(HealRule.created_at)).all()
    )


def create_heal_rule(
    session: Session,
    name: str,
    description: Optional[str],
    alert_rule_id: int,
    action_id: str,
    action_params: dict,
    enabled: bool,
    auto_execute: bool,
    priority: int,
    verification_config: Optional[dict] = None,
) -> HealRule:
    """创建自愈规则。"""
    if not name or len(name) > 128:
        raise ValueError("规则名称长度必须在 1-128 字符之间")
    if not action_id or len(action_id) > 128:
        raise ValueError("action_id 长度必须在 1-128 字符之间")
    if not isinstance(action_params, dict):
        raise ValueError("action_params 必须是 JSON 对象")

    # alert_rule_id 必须存在
    alert_rule = session.get(AlertRule, alert_rule_id)
    if alert_rule is None:
        raise ValueError(f"告警规则不存在: alert_rule_id={alert_rule_id}")

    # action_id 必须在动作库中存在
    action = session.exec(
        select(HealAction).where(HealAction.action_id == action_id)
    ).first()
    if action is None:
        raise ValueError(f"自愈动作不存在: action_id={action_id}")

    # 同一 alert_rule_id 只能有一条自愈规则（MVP 设计）
    existing = session.exec(
        select(HealRule).where(HealRule.alert_rule_id == alert_rule_id)
    ).first()
    if existing is not None:
        raise ValueError(
            f"告警规则 {alert_rule_id} 已存在自愈规则"
        )

    rule = HealRule(
        name=name,
        description=description,
        alert_rule_id=alert_rule_id,
        action_id=action_id,
        action_params=json.dumps(action_params, ensure_ascii=False),
        enabled=enabled,
        auto_execute=auto_execute,
        priority=priority,
        verification_config=json.dumps(verification_config, ensure_ascii=False)
        if verification_config is not None
        else None,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)

    logger.info(f"创建自愈规则: id={rule.id}, alert_rule_id={alert_rule_id}")
    return rule


def update_heal_rule(
    session: Session,
    rule: HealRule,
    name: Optional[str] = None,
    description: Optional[str] = None,
    action_id: Optional[str] = None,
    action_params: Optional[dict] = None,
    enabled: Optional[bool] = None,
    auto_execute: Optional[bool] = None,
    priority: Optional[int] = None,
    verification_config: Optional[dict] = None,
) -> HealRule:
    """更新自愈规则。"""
    if name is not None:
        if not name or len(name) > 128:
            raise ValueError("规则名称长度必须在 1-128 字符之间")
        rule.name = name
    if description is not None:
        rule.description = description
    if action_id is not None:
        action = session.exec(
            select(HealAction).where(HealAction.action_id == action_id)
        ).first()
        if action is None:
            raise ValueError(f"自愈动作不存在: action_id={action_id}")
        rule.action_id = action_id
    if action_params is not None:
        if not isinstance(action_params, dict):
            raise ValueError("action_params 必须是 JSON 对象")
        rule.action_params = json.dumps(action_params, ensure_ascii=False)
    if enabled is not None:
        rule.enabled = enabled
    if auto_execute is not None:
        rule.auto_execute = auto_execute
    if priority is not None:
        rule.priority = priority
    if verification_config is not None:
        if not isinstance(verification_config, dict):
            raise ValueError("verification_config 必须是 JSON 对象")
        rule.verification_config = json.dumps(
            verification_config, ensure_ascii=False
        )

    rule.updated_at = datetime.now(timezone.utc)
    session.add(rule)
    session.commit()
    session.refresh(rule)

    logger.info(f"更新自愈规则: id={rule.id}")
    return rule


def delete_heal_rule(session: Session, rule: HealRule) -> None:
    """删除自愈规则。

    删除规则不影响历史执行记录。
    """
    session.delete(rule)
    session.commit()
    logger.info(f"删除自愈规则: id={rule.id}")


def toggle_heal_rule(session: Session, rule: HealRule) -> HealRule:
    """切换规则启用/禁用状态。"""
    rule.enabled = not rule.enabled
    rule.updated_at = datetime.now(timezone.utc)
    session.add(rule)
    session.commit()
    session.refresh(rule)
    logger.info(f"切换自愈规则状态: id={rule.id}, enabled={rule.enabled}")
    return rule
