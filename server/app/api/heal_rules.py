"""自愈规则 API 路由。"""

import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from app.api.deps import get_db, require_admin
from app.models.heal_rule import HealRule
from app.models.user import User
from app.schemas.heal_rule import HealRuleCreate, HealRuleRead, HealRuleUpdate
from app.services import heal_rule as heal_rule_service

router = APIRouter(prefix="/api/v1/heal-rules", tags=["heal-rules"])

class ToggleResponse(BaseModel):
    """启用/禁用切换响应。"""

    id: int
    enabled: bool


def _rule_to_read(rule: HealRule) -> HealRuleRead:
    """将 HealRule ORM 对象转换为响应模型。"""
    return HealRuleRead(
        id=rule.id,
        name=rule.name,
        description=rule.description,
        alert_rule_id=rule.alert_rule_id,
        action_id=rule.action_id,
        action_params=json.loads(rule.action_params),
        enabled=rule.enabled,
        auto_execute=rule.auto_execute,
        priority=rule.priority,
        verification_config=json.loads(rule.verification_config)
        if rule.verification_config
        else None,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


@router.get("", response_model=list[HealRuleRead])
def list_heal_rules(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """列出所有自愈规则。"""
    rules = heal_rule_service.list_heal_rules(db)
    return [_rule_to_read(r) for r in rules]


@router.post("", response_model=HealRuleRead, status_code=status.HTTP_201_CREATED)
def create_heal_rule(
    body: HealRuleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """创建自愈规则（admin）。"""
    try:
        rule = heal_rule_service.create_heal_rule(
            db,
            name=body.name,
            description=body.description,
            alert_rule_id=body.alert_rule_id,
            action_id=body.action_id,
            action_params=body.action_params,
            enabled=body.enabled,
            auto_execute=body.auto_execute,
            priority=body.priority,
            verification_config=body.verification_config,
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

    return _rule_to_read(rule)


@router.get("/{rule_id}", response_model=HealRuleRead)
def get_heal_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """查看自愈规则详情。"""
    rule = heal_rule_service.get_heal_rule_by_id(db, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="规则不存在",
        )
    return _rule_to_read(rule)


@router.put("/{rule_id}", response_model=HealRuleRead)
def update_heal_rule(
    rule_id: int,
    body: HealRuleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """更新自愈规则（admin）。"""
    rule = heal_rule_service.get_heal_rule_by_id(db, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="规则不存在",
        )

    try:
        update_data = body.model_dump(exclude_unset=True)
        rule = heal_rule_service.update_heal_rule(db, rule, **update_data)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return _rule_to_read(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_heal_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """删除自愈规则（admin）。"""
    rule = heal_rule_service.get_heal_rule_by_id(db, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="规则不存在",
        )

    heal_rule_service.delete_heal_rule(db, rule)
    return None


@router.post("/{rule_id}/toggle", response_model=ToggleResponse)
def toggle_heal_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """启用/禁用自愈规则（admin）。"""
    rule = heal_rule_service.get_heal_rule_by_id(db, rule_id)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="规则不存在",
        )

    rule = heal_rule_service.toggle_heal_rule(db, rule)
    return ToggleResponse(id=rule.id, enabled=rule.enabled)
