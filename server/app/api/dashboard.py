"""Dashboard 首页数据 API。"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, func, select

from app.core.utils import ensure_utc, now_utc
from app.api.deps import get_current_user, get_db
from app.models.alert_event import AlertEvent
from app.models.alert_state import AlertState
from app.models.incident import Incident
from app.models.node import Node
from app.models.user import User

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

# 节点在线判定窗口：last_seen 在 5 分钟内视为在线
_NODE_ONLINE_THRESHOLD_SECONDS = 300


def _today_start() -> datetime:
    now = now_utc()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@router.get("/summary")
def get_dashboard_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """首页汇总数据：节点状态、告警统计、最近事件。"""
    try:
        node_status = _get_node_status(db)
        alert_stats = _get_alert_stats(db)
        recent_events = _get_recent_events(db)
        return {
            "node_status": node_status,
            "alert_stats": alert_stats,
            "recent_events": recent_events,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取首页数据失败: {exc}",
        )


@router.get("/recent-events")
def get_recent_events_endpoint(
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """最近事件列表。"""
    try:
        events = _get_recent_events(db, limit=limit)
        return {"events": events}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取最近事件失败: {exc}",
        )


@router.get("/alert-stats")
def get_alert_stats_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """告警统计。"""
    try:
        return _get_alert_stats(db)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取告警统计失败: {exc}",
        )


def _get_node_status(db: Session) -> dict:
    """计算节点在线状态。"""
    now = now_utc()
    threshold = now - timedelta(seconds=_NODE_ONLINE_THRESHOLD_SECONDS)

    nodes = db.exec(
        select(Node).where(Node.is_deleted == False)  # noqa: E712
    ).all()

    total = len(nodes)
    online = 0
    offline = 0
    recent_offline = []

    for node in nodes:
        last_seen = ensure_utc(node.last_seen)
        # 显式 status 为 online 且 last_seen 在窗口内才算在线
        if node.status == "online" and last_seen is not None and last_seen >= threshold:
            online += 1
        else:
            offline += 1
            recent_offline.append({
                "node_id": node.node_id,
                "name": node.name,
                "status": node.status,
                "last_seen": last_seen.isoformat().replace("+00:00", "Z") if last_seen else None,
            })

    # 最近离线节点限制 5 个
    recent_offline = sorted(
        recent_offline,
        key=lambda x: x["last_seen"] or "",
        reverse=True,
    )[:5]

    return {
        "total": total,
        "online": online,
        "offline": offline,
        "recent_offline": recent_offline,
    }


def _get_alert_stats(db: Session) -> dict:
    """统计当前 firing 告警与今日新增。"""
    today_start = _today_start()

    firing_count = db.exec(
        select(func.count(AlertState.id)).where(
            AlertState.state == "firing",
        )
    ).one() or 0

    # 按严重度统计 firing 告警
    critical = 0
    warning = 0
    info = 0
    firing_rows = db.exec(
        select(AlertState).where(AlertState.state == "firing")
    ).all()
    for state in firing_rows:
        from app.models.alert_rule import AlertRule
        alert_rule = db.get(AlertRule, state.rule_id)
        severity = alert_rule.severity if alert_rule else "warning"
        if severity == "critical":
            critical += 1
        elif severity == "warning":
            warning += 1
        elif severity == "info":
            info += 1

    today_new = db.exec(
        select(func.count(AlertEvent.id)).where(
            AlertEvent.fired_at >= today_start,
        )
    ).one() or 0

    return {
        "firing": firing_count,
        "critical": critical,
        "warning": warning,
        "info": info,
        "today_new": today_new,
    }


def _get_recent_events(db: Session, limit: int = 10) -> list[dict]:
    """获取最近事件列表。"""
    incidents = db.exec(
        select(Incident)
        .where(Incident.is_deleted == False)  # noqa: E712
        .order_by(Incident.started_at.desc())
        .limit(limit)
    ).all()

    events = []
    for incident in incidents:
        started_at = ensure_utc(incident.started_at)
        events.append({
            "id": incident.id,
            "type": "incident",
            "title": incident.title,
            "status": incident.status,
            "severity": incident.severity,
            "timestamp": started_at.isoformat().replace("+00:00", "Z") if started_at else None,
        })
    return events
