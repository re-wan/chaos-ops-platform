"""事件时间线查询服务。"""

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.core.incident_event_bus import record_incident_event
from app.models.incident_event import IncidentEvent


def list_timeline_events(
    session: Session,
    incident_id: int,
    event_type: Optional[str] = None,
) -> list[IncidentEvent]:
    """查询事件时间线，按 timestamp 倒序排列（最新在前）。"""
    query = select(IncidentEvent).where(
        IncidentEvent.incident_id == incident_id
    )
    if event_type is not None:
        query = query.where(IncidentEvent.event_type == event_type)
    query = query.order_by(IncidentEvent.timestamp.desc())
    return list(session.exec(query).all())


def add_timeline_note(
    session: Session,
    incident_id: int,
    content: str,
    created_by: int,
) -> IncidentEvent:
    """添加人工注释到事件时间线。"""
    return record_incident_event(
        session=session,
        incident_id=incident_id,
        event_type="user",
        event_subtype="incident.note_added",
        title="timeline.incident.noteAdded.title",
        # 注释内容是用户输入，不翻译，原样存储
        description=content,
        source="user",
        created_by=created_by,
        timestamp=datetime.now(timezone.utc),
    )
