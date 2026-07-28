"""自愈任务、待确认任务、白名单 API 路由。"""

import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from app.api.deps import get_current_user, get_db, get_pagination, require_admin
from app.schemas.page import Page, PageParams
from app.models.heal_task import HealTask
from app.models.heal_whitelist import HealAutoApproveWhitelist
from app.models.user import User
from app.schemas.heal_task import (
    HealTaskRead,
    HealTaskVerificationRead,
)
from app.services import heal_verification
from app.schemas.heal_whitelist import HealWhitelistCreate, HealWhitelistRead
from app.services import heal_executor, heal_task_queue

router = APIRouter(prefix="/api/v1", tags=["heal-tasks"])


class ApproveRequest(BaseModel):
    """确认任务请求体。"""

    add_to_whitelist: bool = False


class RejectRequest(BaseModel):
    """拒绝任务请求体。"""

    reason: str | None = None

def _task_to_read(task: HealTask) -> HealTaskRead:
    """将 HealTask ORM 对象转换为响应模型。"""
    return HealTaskRead(
        id=task.id,
        task_id=task.task_id,
        node_id=task.node_id,
        alert_rule_id=task.alert_rule_id,
        heal_rule_id=task.heal_rule_id,
        action_id=task.action_id,
        action_params=json.loads(task.action_params),
        status=task.status,
        risk_level=task.risk_level,
        requires_approval=task.requires_approval,
        approved_by=task.approved_by,
        approved_at=task.approved_at,
        executed_by=task.executed_by,
        started_at=task.started_at,
        finished_at=task.finished_at,
        result=json.loads(task.result) if task.result else None,
        error_message=task.error_message,
        verification_config=json.loads(task.verification_config)
        if task.verification_config
        else None,
        verification_status=task.verification_status,
        verification_result=json.loads(task.verification_result)
        if task.verification_result
        else None,
        verification_due_at=task.verification_due_at,
        retry_count=task.retry_count,
        max_retries=task.max_retries,
        scheduled_at=task.scheduled_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _whitelist_to_read(entry: HealAutoApproveWhitelist) -> HealWhitelistRead:
    """将白名单 ORM 对象转换为响应模型。"""
    return HealWhitelistRead(
        id=entry.id,
        node_id=entry.node_id,
        action_id=entry.action_id,
        enabled=entry.enabled,
        created_by=entry.created_by,
        created_at=entry.created_at,
    )


# ---- 待确认任务 ----


@router.get("/heal-pending-tasks", response_model=Page[HealTaskRead])
def list_pending_tasks(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    pagination: PageParams = Depends(get_pagination),
):
    """列出待确认任务，支持服务端分页。"""
    tasks = heal_task_queue.list_pending_tasks(db)
    total = len(tasks)
    start = (pagination.page - 1) * pagination.page_size
    end = start + pagination.page_size
    paged_tasks = tasks[start:end]
    return Page[HealTaskRead](
        items=[_task_to_read(t) for t in paged_tasks],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("/heal-pending-tasks/{task_id}/approve", response_model=HealTaskRead)
def approve_pending_task(
    task_id: int,
    body: ApproveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """确认并执行待确认任务（admin）。"""

    task = heal_task_queue.get_task_by_id(db, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在",
        )

    try:
        task = heal_executor.approve_and_execute(
            db,
            task,
            approved_by=current_user.id,
            add_to_whitelist=body.add_to_whitelist,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return _task_to_read(task)


@router.post("/heal-pending-tasks/{task_id}/reject", response_model=HealTaskRead)
def reject_pending_task(
    task_id: int,
    body: RejectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """拒绝待确认任务（admin）。"""

    task = heal_task_queue.get_task_by_id(db, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在",
        )

    try:
        task = heal_executor.reject_execution(
            db,
            task,
            rejected_by=current_user.id,
            reason=body.reason,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return _task_to_read(task)


# ---- 任务管理 ----


@router.get("/heal-tasks", response_model=Page[HealTaskRead])
def list_heal_tasks(
    node_id: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    pagination: PageParams = Depends(get_pagination),
):
    """列出自愈任务，支持服务端分页。"""
    filters = []
    if node_id is not None:
        filters.append(HealTask.node_id == node_id)
    if status is not None:
        filters.append(HealTask.status == status)

    total = db.exec(
        select(func.count(HealTask.id)).where(*filters)
    ).one() or 0

    start = (pagination.page - 1) * pagination.page_size
    tasks = db.exec(
        select(HealTask)
        .where(*filters)
        # id 作为 tiebreaker，保证同 created_at 时翻页稳定不重复不漏
        .order_by(HealTask.created_at, HealTask.id)
        .offset(start)
        .limit(pagination.page_size)
    ).all()
    return Page[HealTaskRead](
        items=[_task_to_read(t) for t in tasks],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("/heal-tasks/{task_id}/retry", response_model=HealTaskRead)
def retry_heal_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """重试失败/超时任务（admin）。"""

    task = heal_task_queue.get_task_by_id(db, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在",
        )

    try:
        task = heal_executor.retry_failed_task(db, task)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return _task_to_read(task)


@router.get("/heal-tasks/{task_id}/verification")
def get_task_verification(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查看任务验证结果。"""
    task = heal_task_queue.get_task_by_id(db, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在",
        )

    status_dict = heal_verification.get_verification_status(task)
    return HealTaskVerificationRead(**status_dict)


@router.post("/heal-tasks/{task_id}/verify")
def trigger_task_verification(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """手动触发任务效果验证（admin）。"""

    task = heal_task_queue.get_task_by_id(db, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在",
        )

    if task.status != "success":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只有执行成功的任务才能验证",
        )

    try:
        task = heal_verification.verify_task(db, task)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return HealTaskVerificationRead(
        **heal_verification.get_verification_status(task)
    )


# ---- 白名单 ----


@router.get("/heal-whitelist", response_model=list[HealWhitelistRead])
def list_whitelist(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """列出白名单。"""
    entries = db.exec(
        select(HealAutoApproveWhitelist).order_by(HealAutoApproveWhitelist.created_at)
    ).all()
    return [_whitelist_to_read(e) for e in entries]


@router.post(
    "/heal-whitelist", response_model=HealWhitelistRead, status_code=status.HTTP_201_CREATED
)
def create_whitelist_entry(
    body: HealWhitelistCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """手动添加白名单（admin）。"""

    existing = db.exec(
        select(HealAutoApproveWhitelist).where(
            HealAutoApproveWhitelist.node_id == body.node_id,
            HealAutoApproveWhitelist.action_id == body.action_id,
        )
    ).first()

    if existing is not None:
        existing.enabled = True
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return _whitelist_to_read(existing)

    entry = HealAutoApproveWhitelist(
        node_id=body.node_id,
        action_id=body.action_id,
        enabled=True,
        created_by=current_user.id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _whitelist_to_read(entry)


@router.delete("/heal-whitelist/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_whitelist_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """移除白名单（admin）。"""

    entry = db.get(HealAutoApproveWhitelist, entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="白名单不存在",
        )

    db.delete(entry)
    db.commit()
    return None
