"""告警规则 API 路由。"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from app.api.deps import get_current_user, get_db, get_pagination, require_admin
from app.schemas.page import Page, PageParams
from app.models.alert_rule import AlertRule
from app.models.user import User
from app.schemas.alert_rule import AlertRuleCreate, AlertRuleRead, AlertRuleUpdate
from app.services import alert_rule as alert_rule_service
from app.services import audit_log

router = APIRouter(prefix="/api/v1/alert-rules", tags=["alert-rules"])


class ToggleResponse(BaseModel):
    """启用/禁用切换响应。"""

    id: int
    enabled: bool


def _rule_to_read(rule: AlertRule) -> AlertRuleRead:
    """将 AlertRule ORM 对象转换为响应模型。"""
    return AlertRuleRead(
        id=rule.id,
        name=rule.name,
        description=rule.description,
        scope=rule.scope,
        scope_target=rule.scope_target,
        condition_type=rule.condition_type,
        condition=rule.condition,
        pending_duration_seconds=rule.pending_duration_seconds,
        resolve_duration_seconds=rule.resolve_duration_seconds,
        severity=rule.severity,
        enabled=rule.enabled,
        notification_channel_ids=alert_rule_service._parse_channel_ids(
            rule.notification_channel_ids
        ),
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


@router.get("", response_model=Page[AlertRuleRead])
def list_alert_rules(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    pagination: PageParams = Depends(get_pagination),
):
    """列出所有告警规则，支持服务端分页。"""
    total = db.exec(select(func.count(AlertRule.id))).one() or 0
    start = (pagination.page - 1) * pagination.page_size
    rules = db.exec(
        select(AlertRule)
        # id 作为 tiebreaker，保证同 created_at 时翻页稳定不重复不漏
        .order_by(AlertRule.created_at, AlertRule.id)
        .offset(start)
        .limit(pagination.page_size)
    ).all()
    return Page[AlertRuleRead](
        items=[_rule_to_read(r) for r in rules],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("", response_model=AlertRuleRead, status_code=status.HTTP_201_CREATED)
def create_alert_rule(
    body: AlertRuleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """创建告警规则。"""
    try:
        rule = alert_rule_service.create_alert_rule(
            db,
            name=body.name,
            description=body.description,
            scope=body.scope,
            scope_target=body.scope_target,
            condition_type=body.condition_type,
            condition=body.condition,
            pending_duration_seconds=body.pending_duration_seconds,
            resolve_duration_seconds=body.resolve_duration_seconds,
            severity=body.severity,
            enabled=body.enabled,
            notification_channel_ids=body.notification_channel_ids,
        )
    except ValueError as e:
        detail = str(e)
        if "已存在" in detail or "License" in detail:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
        )

    # 审计：仅成功变更落审计；底层异常隔离，失败只记日志不阻断业务。
    audit_log.record_alert_rule_created(db, rule, created_by=current_user.id)
    return _rule_to_read(rule)


@router.get("/{rule_id}", response_model=AlertRuleRead)
def get_alert_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取告警规则详情。"""
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="规则不存在",
        )
    return _rule_to_read(rule)


@router.put("/{rule_id}", response_model=AlertRuleRead)
def update_alert_rule(
    rule_id: int,
    body: AlertRuleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """更新告警规则。"""
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="规则不存在",
        )

    changed_fields = list(body.model_dump(exclude_unset=True).keys())
    try:
        rule = alert_rule_service.update_alert_rule(
            db,
            rule,
            **body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        detail = str(e)
        if "已存在" in detail:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
        )

    audit_log.record_alert_rule_updated(
        db, rule, changed_fields=changed_fields, created_by=current_user.id
    )
    return _rule_to_read(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alert_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """删除告警规则。"""
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="规则不存在",
        )

    # 删除前先留存标识字段（删除后 ORM 属性过期不可取），供审计使用。
    deleted_name = rule.name
    try:
        alert_rule_service.delete_alert_rule(db, rule)
    except ValueError as e:
        # 仍被自愈规则引用 → 冲突，提示先解绑（失败变更不落审计）
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    audit_log.record_alert_rule_deleted(
        db, rule_id=rule_id, name=deleted_name, created_by=current_user.id
    )
    return None


@router.post("/{rule_id}/toggle", response_model=AlertRuleRead)
def toggle_alert_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """启用/禁用告警规则。"""
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="规则不存在",
        )

    rule = alert_rule_service.toggle_alert_rule(db, rule)
    return _rule_to_read(rule)
