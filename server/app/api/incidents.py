"""事件（Incident）API 路由。"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session

from app.core.error_messages import error_detail, error_detail_from_exception
from app.api.deps import get_current_user, get_db, get_pagination
from app.schemas.page import Page, PageParams
from app.models.user import User
from app.schemas.incident import (
    IncidentCreate,
    IncidentDetailRead,
    IncidentMergeRequest,
    IncidentRead,
    IncidentUpdate,
)
from app.schemas.incident_alert import IncidentAlertRead
from app.schemas.incident_event import IncidentEventNoteCreate, IncidentEventRead
from app.services import incident as incident_service
from app.services import incident_timeline as timeline_service

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])


class StatusResponse(BaseModel):
    """状态操作响应包装。"""

    id: int
    status: str


def _incident_to_read(incident) -> IncidentRead:
    """将 Incident ORM 对象转换为响应模型。"""
    return IncidentRead(
        id=incident.id,
        title=incident.title,
        description=incident.description,
        severity=incident.severity,
        status=incident.status,
        source=incident.source,
        created_by=incident.created_by,
        assigned_to=incident.assigned_to,
        rule_id=incident.rule_id,
        node_id=incident.node_id,
        started_at=incident.started_at,
        acknowledged_at=incident.acknowledged_at,
        resolved_at=incident.resolved_at,
        closed_at=incident.closed_at,
        is_deleted=incident.is_deleted,
        created_at=incident.created_at,
        updated_at=incident.updated_at,
    )


def _alert_to_read(alert, db: Session) -> IncidentAlertRead:
    """将 IncidentAlert ORM 对象转换为包含告警事件详情的响应模型。"""
    import json

    from app.models.alert_event import AlertEvent
    from app.schemas.incident_alert import AlertEventRead

    event = db.get(AlertEvent, alert.alert_event_id)
    alert_event_read = None
    if event is not None:
        labels = None
        if event.labels:
            try:
                labels = json.loads(event.labels)
            except json.JSONDecodeError:
                labels = None
        alert_event_read = AlertEventRead(
            id=event.id,
            rule_id=event.rule_id,
            node_id=event.node_id,
            severity=event.severity,
            message=event.message,
            labels=labels,
            fired_at=event.fired_at,
        )

    return IncidentAlertRead(
        id=alert.id,
        incident_id=alert.incident_id,
        alert_event_id=alert.alert_event_id,
        added_at=alert.added_at,
        alert_event=alert_event_read,
    )


@router.get("", response_model=Page[IncidentRead])
def list_incidents(
    status: Optional[str] = None,
    severity: Optional[str] = None,
    node_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    pagination: PageParams = Depends(get_pagination),
):
    """列出事件，支持 status/severity/node_id 过滤与服务端分页。"""
    incidents = incident_service.list_incidents(
        db, status=status, severity=severity, node_id=node_id
    )
    total = len(incidents)
    start = (pagination.page - 1) * pagination.page_size
    end = start + pagination.page_size
    paged_incidents = incidents[start:end]
    return Page[IncidentRead](
        items=[_incident_to_read(i) for i in paged_incidents],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("", response_model=IncidentRead, status_code=status.HTTP_201_CREATED)
def create_incident(
    request: Request,
    body: IncidentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """手动创建事件。"""
    try:
        incident = incident_service.create_manual_incident(
            db,
            title=body.title,
            severity=body.severity,
            description=body.description,
            assigned_to=body.assigned_to,
            alert_event_ids=body.alert_event_ids,
            created_by=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail_from_exception(request, e),
        )

    return _incident_to_read(incident)


@router.get("/{incident_id}", response_model=IncidentDetailRead)
def get_incident(
    request: Request,
    incident_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查看事件详情（包含关联告警列表）。"""
    incident, alerts = incident_service.get_incident_with_alerts(db, incident_id)
    if incident is None or incident.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "incidents.not_found"),
        )

    detail = IncidentDetailRead(**_incident_to_read(incident).model_dump())
    detail.alerts = [_alert_to_read(a, db) for a in alerts]
    return detail


@router.put("/{incident_id}", response_model=IncidentRead)
def update_incident(
    request: Request,
    incident_id: int,
    body: IncidentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新事件（标题、描述、负责人等）。"""
    incident = incident_service.get_incident(db, incident_id)
    if incident is None or incident.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "incidents.not_found"),
        )

    try:
        incident = incident_service.update_incident(
            db,
            incident,
            title=body.title,
            description=body.description,
            assigned_to=body.assigned_to,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail_from_exception(request, e),
        )

    return _incident_to_read(incident)


@router.delete("/{incident_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_incident(
    request: Request,
    incident_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """软删除事件。"""
    incident = incident_service.get_incident(db, incident_id)
    if incident is None or incident.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "incidents.not_found"),
        )

    incident_service.delete_incident(db, incident)
    return None


@router.post("/{incident_id}/acknowledge", response_model=IncidentRead)
def acknowledge_incident(
    request: Request,
    incident_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """认领事件。"""
    incident = incident_service.get_incident(db, incident_id)
    if incident is None or incident.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "incidents.not_found"),
        )

    try:
        incident = incident_service.acknowledge_incident(
            db, incident, current_user.id
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail_from_exception(request, e),
        )

    return _incident_to_read(incident)


@router.post("/{incident_id}/resolve", response_model=IncidentRead)
def resolve_incident(
    request: Request,
    incident_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """标记事件已解决。"""
    incident = incident_service.get_incident(db, incident_id)
    if incident is None or incident.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "incidents.not_found"),
        )

    try:
        incident = incident_service.resolve_incident(db, incident, current_user.id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail_from_exception(request, e),
        )

    return _incident_to_read(incident)


@router.post("/{incident_id}/close", response_model=IncidentRead)
def close_incident(
    request: Request,
    incident_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """关闭事件。"""
    incident = incident_service.get_incident(db, incident_id)
    if incident is None or incident.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "incidents.not_found"),
        )

    try:
        incident = incident_service.close_incident(db, incident, current_user.id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail_from_exception(request, e),
        )

    return _incident_to_read(incident)


@router.get("/{incident_id}/timeline", response_model=list[IncidentEventRead])
def get_incident_timeline(
    request: Request,
    incident_id: int,
    event_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取事件时间线（按时间倒序）。"""
    incident = incident_service.get_incident(db, incident_id)
    if incident is None or incident.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "incidents.not_found"),
        )

    events = timeline_service.list_timeline_events(
        db, incident_id=incident_id, event_type=event_type
    )
    return events


@router.post(
    "/{incident_id}/timeline/notes",
    response_model=IncidentEventRead,
    status_code=status.HTTP_201_CREATED,
)
def add_incident_timeline_note(
    request: Request,
    incident_id: int,
    body: IncidentEventNoteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """添加人工注释到事件时间线。"""
    incident = incident_service.get_incident(db, incident_id)
    if incident is None or incident.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "incidents.not_found"),
        )

    event = timeline_service.add_timeline_note(
        db,
        incident_id=incident_id,
        content=body.content,
        created_by=current_user.id,
    )
    return event


@router.post("/{incident_id}/merge", response_model=IncidentRead)
def merge_incident(
    request: Request,
    incident_id: int,
    body: IncidentMergeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """合并事件。"""
    source_incident = incident_service.get_incident(db, incident_id)
    if source_incident is None or source_incident.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "incidents.merge_source_not_found"),
        )

    target_incident = incident_service.get_incident(db, body.target_incident_id)
    if target_incident is None or target_incident.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "incidents.merge_target_not_found"),
        )

    try:
        incident_service.merge_incident(db, source_incident, target_incident)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail_from_exception(request, e),
        )

    return _incident_to_read(source_incident)
