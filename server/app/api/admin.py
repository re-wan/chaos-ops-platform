"""管理后台 API 路由。"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.api import users as users_api
from app.api.deps import get_db, require_admin
from app.core.config import settings
from app.core.error_messages import error_detail
from app.core.utils import now_utc
from app.models.manual_order import ManualOrder
from app.models.user import User
from app.schemas.manual_order import ManualOrderCreate, ManualOrderRead
from app.schemas.page import Page
from app.schemas.user import UserRead
from app.services.alert_detector import (
    evaluate_rule_for_node_from_storage,
    get_alert_detector,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/alert-detector/stats")
def get_detector_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> dict:
    """获取告警检测器统计信息（admin）。"""
    detector = get_alert_detector()
    if detector is None:
        return {
            "rules_indexed": 0,
            "rules_skipped": 0,
            "cache": {
                "nodes": 0,
                "metrics": 0,
                "entries": 0,
                "hits": 0,
                "misses": 0,
                "ttl_seconds": 0,
                "max_entries": 0,
            },
            "processed_events": 0,
            "error_count": 0,
            "reload_failure_count": 0,
            "last_latency_ms": 0.0,
        }
    return detector.stats()


@router.post("/alert-detector/evaluate/{rule_id}")
def manual_evaluate_rule(
    rule_id: int,
    node_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> dict:
    """手动触发单条规则对指定节点进行评估（admin）。

    数据取自时序库最近窗口样本（与实时检测同源、跨 worker 一致）；
    窗口内无任何样本时 ``no_data`` 为 True，``state`` 为当前持久化状态
    （不做状态转换，避免"无数据"被误判为"条件不满足"）。
    """
    try:
        new_state, sample_count = evaluate_rule_for_node_from_storage(
            session=db,
            rule_id=rule_id,
            node_id=node_id,
            now=now_utc(),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return {
        "rule_id": rule_id,
        "node_id": node_id,
        "state": new_state,
        "evaluated_at": now_utc().isoformat(),
        "no_data": sample_count == 0,
        "sample_count": sample_count,
    }


# ---- 兼容路径：/api/v1/admin/users 保留为 /api/v1/users 的别名 ----
# 业务逻辑统一由 app.api.users 提供，避免代码重复。

router.add_api_route(
    "/users",
    users_api.list_users,
    methods=["GET"],
    response_model=Page[UserRead],
    tags=["admin"],
)
router.add_api_route(
    "/users",
    users_api.create_user,
    methods=["POST"],
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    tags=["admin"],
)
router.add_api_route(
    "/users/{user_id}",
    users_api.get_user,
    methods=["GET"],
    response_model=UserRead,
    tags=["admin"],
)
router.add_api_route(
    "/users/{user_id}",
    users_api.update_user,
    methods=["PUT"],
    response_model=UserRead,
    tags=["admin"],
)
router.add_api_route(
    "/users/{user_id}",
    users_api.delete_user,
    methods=["DELETE"],
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["admin"],
)


# ---- 本地人工订单（人工收款通道） ----


@router.post(
    "/manual-orders",
    response_model=ManualOrderRead,
    status_code=status.HTTP_201_CREATED,
)
def create_manual_order(
    body: ManualOrderCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> ManualOrder:
    """录入人工订单（admin）。

    人工收款（对公账户/微信/支付宝）确认到账后录入订单号+邮箱，
    客户即可凭此在自助页生成 License。
    """
    existing = db.exec(
        select(ManualOrder).where(ManualOrder.order_id == body.order_id)
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail(request, "订单号已存在"),
        )

    order = ManualOrder(
        order_id=body.order_id,
        email=body.email,
        note=body.note,
    )
    db.add(order)
    try:
        db.commit()
    except IntegrityError:
        # 并发兜底：唯一约束拦截，session 已不可用，必须 rollback
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail(request, "订单号已存在（唯一约束冲突）"),
        )
    db.refresh(order)
    return order


@router.get("/manual-orders", response_model=list[ManualOrderRead])
def list_manual_orders(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> list[ManualOrder]:
    """列出全部人工订单（admin，按录入时间倒序）。"""
    return list(
        db.exec(select(ManualOrder).order_by(ManualOrder.created_at.desc())).all()
    )


@router.get("/settings")
def get_settings(
    current_user: User = Depends(require_admin),
) -> dict:
    """获取系统配置（admin，只读）。

    Phase 1 仅提供只读展示；Phase 2 再支持运行时修改。
    """
    # 仅暴露非敏感、可展示的配置项
    return {
        "public_server_url": settings.PUBLIC_SERVER_URL,
        "alert_detector_enabled": settings.ALERT_DETECTOR_ENABLED,
        "metric_backend_health_check_at_startup": settings.METRIC_BACKEND_HEALTH_CHECK_AT_STARTUP,
        "local_agent_enabled": settings.LOCAL_AGENT_ENABLED,
        "log_dir": settings.LOG_DIR,
        "backup_dir": settings.BACKUP_DIR,
        "influxdb_url": settings.INFLUXDB_URL,
        "influxdb_org": settings.INFLUXDB_ORG,
        "influxdb_bucket": settings.INFLUXDB_BUCKET,
        "influxdb_retention_days": settings.INFLUXDB_RETENTION_DAYS,
        "user_session_max_age_days": settings.USER_SESSION_MAX_AGE_DAYS,
        "user_session_slide_days": settings.USER_SESSION_SLIDE_DAYS,
        # Phase 3：用于前端在功能关闭时给出明确提示（默认关闭）。
        "ai_self_optimization_enabled": settings.AI_SELF_OPTIMIZATION_ENABLED,
        "optimization_effect_track_days": settings.OPTIMIZATION_EFFECT_TRACK_DAYS,
    }
