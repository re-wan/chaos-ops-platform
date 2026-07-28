"""告警静默规则业务逻辑。"""

import json
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.core.logger import get_logger
from app.models.alert_silence import AlertSilence

logger = get_logger("audit.alert_silence")


def _parse_matchers(matchers_str: str) -> dict:
    """解析 matchers JSON 字符串，失败返回空字典。"""
    try:
        data = json.loads(matchers_str)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {}


def is_silenced(
    session: Session,
    labels: Optional[dict],
    now: datetime,
) -> bool:
    """判断给定 labels 是否被任意有效静默规则匹配。"""
    labels = labels or {}
    active_silences = session.exec(
        select(AlertSilence).where(
            AlertSilence.starts_at <= now,
            AlertSilence.ends_at >= now,
        )
    ).all()

    for silence in active_silences:
        matchers = _parse_matchers(silence.matchers)
        if all(labels.get(k) == v for k, v in matchers.items()):
            return True

    return False


def list_active_silences(
    session: Session,
    now: datetime,
) -> list[AlertSilence]:
    """列出当前生效的静默规则。"""
    return list(
        session.exec(
            select(AlertSilence).where(
                AlertSilence.starts_at <= now,
                AlertSilence.ends_at >= now,
            )
        ).all()
    )


def list_silences(session: Session) -> list[AlertSilence]:
    """列出所有静默规则（含已过期）。"""
    return list(
        session.exec(select(AlertSilence).order_by(AlertSilence.created_at)).all()
    )


def create_silence(
    session: Session,
    matchers: dict,
    starts_at: datetime,
    ends_at: datetime,
    comment: Optional[str] = None,
    created_by: Optional[int] = None,
) -> AlertSilence:
    """创建静默规则。"""
    if not isinstance(matchers, dict):
        raise ValueError("matchers 必须是 JSON 对象")
    if ends_at <= starts_at:
        raise ValueError("ends_at 必须晚于 starts_at")

    silence = AlertSilence(
        matchers=json.dumps(matchers, sort_keys=True, ensure_ascii=False),
        starts_at=starts_at,
        ends_at=ends_at,
        comment=comment,
        created_by=created_by,
    )
    session.add(silence)
    session.commit()
    session.refresh(silence)

    logger.info(
        f"创建静默规则: id={silence.id}, matchers={silence.matchers}, "
        f"starts_at={silence.starts_at.isoformat()}, "
        f"ends_at={silence.ends_at.isoformat()}, created_by={created_by}"
    )
    return silence


def delete_silence(session: Session, silence_id: int) -> bool:
    """删除（取消）静默规则。"""
    silence = session.get(AlertSilence, silence_id)
    if silence is None:
        return False

    session.delete(silence)
    session.commit()

    logger.info(f"删除静默规则: id={silence_id}")
    return True
