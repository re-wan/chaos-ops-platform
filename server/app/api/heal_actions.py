"""自愈动作 API 路由。"""

import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from app.api.deps import get_current_user, get_db, require_admin
from app.models.heal_action import HealAction
from app.models.user import User
from app.schemas.heal_action import HealActionCreate, HealActionRead, HealActionUpdate
from app.services import heal_action as heal_action_service

router = APIRouter(prefix="/api/v1/heal-actions", tags=["heal-actions"])


class ApproveResponse(BaseModel):
    """审批响应。"""

    id: int
    action_id: str
    is_approved: bool

def _action_to_read(action: HealAction) -> HealActionRead:
    """将 HealAction ORM 对象转换为响应模型。"""
    return HealActionRead(
        id=action.id,
        action_id=action.action_id,
        name=action.name,
        description=action.description,
        action_type=action.action_type,
        script_content=action.script_content,
        script_hash=action.script_hash,
        interpreter=action.interpreter,
        parameter_schema=json.loads(action.parameter_schema),
        risk_level=action.risk_level,
        is_approved=action.is_approved,
        is_builtin=action.is_builtin,
        created_at=action.created_at,
        updated_at=action.updated_at,
    )


@router.get("", response_model=list[HealActionRead])
def list_heal_actions(
    risk_level: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列出所有动作，支持按 risk_level 过滤。"""
    if risk_level is not None and risk_level not in heal_action_service.VALID_RISK_LEVELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"risk_level 必须是 {heal_action_service.VALID_RISK_LEVELS} 之一",
        )
    actions = heal_action_service.list_heal_actions(db, risk_level=risk_level)
    return [_action_to_read(a) for a in actions]


@router.post("", response_model=HealActionRead, status_code=status.HTTP_201_CREATED)
def create_heal_action(
    body: HealActionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """创建自定义动作（admin）。"""

    try:
        action = heal_action_service.create_custom_action(
            db,
            action_id=body.action_id,
            name=body.name,
            description=body.description,
            action_type=body.action_type,
            script_content=body.script_content,
            interpreter=body.interpreter,
            parameter_schema=body.parameter_schema,
            risk_level=body.risk_level,
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

    return _action_to_read(action)


@router.get("/{action_id}", response_model=HealActionRead)
def get_heal_action(
    action_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查看动作详情。"""
    action = heal_action_service.get_action_by_id(db, action_id)
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="动作不存在",
        )
    return _action_to_read(action)


@router.put("/{action_id}", response_model=HealActionRead)
def update_heal_action(
    action_id: int,
    body: HealActionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """更新自定义动作。"""

    action = heal_action_service.get_action_by_id(db, action_id)
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="动作不存在",
        )

    try:
        action = heal_action_service.update_custom_action(
            db,
            action,
            **body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return _action_to_read(action)


@router.delete("/{action_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_heal_action(
    action_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """删除自定义动作。"""

    action = heal_action_service.get_action_by_id(db, action_id)
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="动作不存在",
        )

    try:
        heal_action_service.delete_custom_action(db, action)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return None


@router.post("/{action_id}/approve", response_model=ApproveResponse)
def approve_heal_action(
    action_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """审批自定义脚本动作（admin）。"""

    action = heal_action_service.get_action_by_id(db, action_id)
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="动作不存在",
        )

    try:
        action = heal_action_service.approve_custom_action(db, action)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return ApproveResponse(
        id=action.id,
        action_id=action.action_id,
        is_approved=action.is_approved,
    )
