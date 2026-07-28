"""告警相关聚合 API 路由。"""

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlmodel import Session, select

from app.api.deps import get_current_user, get_db, get_pagination
from app.models.alert_event import AlertEvent
from app.schemas.page import Page, PageParams
from app.models.user import User
from app.schemas.alert_event import AlertEventRead
from app.services import alert_inhibition as inhibition_service
from app.services import alert_silence as silence_service

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


def _event_to_read(event: AlertEvent) -> AlertEventRead:
    """将 AlertEvent ORM 对象转换为响应模型。"""
    return AlertEventRead(
        id=event.id,
        rule_id=event.rule_id,
        node_id=event.node_id,
        severity=event.severity,
        message=event.message,
        labels=json.loads(event.labels) if event.labels else None,
        fired_at=event.fired_at,
    )


@router.get("", response_model=Page[AlertEventRead])
def list_alert_events(
    rule_id: Optional[int] = Query(default=None, description="按规则 ID 过滤"),
    node_id: Optional[str] = Query(default=None, description="按节点 ID 过滤"),
    severity: Optional[str] = Query(default=None, description="按严重级别过滤"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    pagination: PageParams = Depends(get_pagination),
):
    """列出告警事件（触发历史），支持筛选与服务端分页，按触发时间倒序。"""
    filters = []
    if rule_id is not None:
        filters.append(AlertEvent.rule_id == rule_id)
    if node_id is not None:
        filters.append(AlertEvent.node_id == node_id)
    if severity is not None:
        filters.append(AlertEvent.severity == severity)

    total = db.exec(
        select(func.count(AlertEvent.id)).where(*filters)
    ).one() or 0

    start = (pagination.page - 1) * pagination.page_size
    query = (
        select(AlertEvent)
        .where(*filters)
        # id 倒序作为 tiebreaker，保证同 fired_at 时翻页稳定不重复不漏
        .order_by(AlertEvent.fired_at.desc(), AlertEvent.id.desc())
        .offset(start)
        .limit(pagination.page_size)
    )
    events = db.exec(query).all()
    return Page[AlertEventRead](
        items=[_event_to_read(e) for e in events],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/stats")
def get_alert_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """获取去重/静默/抑制统计信息。"""
    now = datetime.now(timezone.utc)
    active_silences = silence_service.list_active_silences(db, now)
    all_inhibitions = inhibition_service.list_inhibitions(db)

    return {
        "dedup_window_seconds": 300,
        "active_silence_count": len(active_silences),
        "total_silence_count": len(silence_service.list_silences(db)),
        "inhibition_count": len(all_inhibitions),
        "checked_at": now.isoformat(),
    }
