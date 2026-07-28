"""事件生命周期后台任务。"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Engine
from sqlmodel import Session, select

from app.core.database import engine as default_engine
from app.core.logger import get_logger
from app.core.scheduler import safe_task
from app.models.incident import Incident
from app.services import incident as incident_service

logger = get_logger("tasks.incident_task")


def _find_resolved_incidents_to_close(session: Session, cutoff: datetime) -> list[Incident]:
    """查询 resolved 超过 24 小时且未关闭的事件。"""
    return list(
        session.exec(
            select(Incident).where(
                Incident.status == "resolved",
                Incident.resolved_at <= cutoff,
                Incident.is_deleted == False,  # noqa: E712
            )
        ).all()
    )


@safe_task
def auto_close_resolved_incidents(engine: Optional[Engine] = None) -> None:
    """每天检查并自动关闭 resolved 超过 24 小时的事件。

    Args:
        engine: 可选数据库引擎，用于测试注入。
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    engine = engine or default_engine

    with Session(engine) as session:
        incidents = _find_resolved_incidents_to_close(session, cutoff)
        for incident in incidents:
            try:
                incident_service.close_incident(session, incident)
                logger.info(
                    f"事件自动关闭: incident_id={incident.id}, "
                    f"resolved_at={incident.resolved_at}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    f"事件自动关闭失败: incident_id={incident.id}, error={exc}"
                )
