"""自愈任务队列与状态机。

负责任务创建、状态转换、Agent 任务分配。
"""

import json
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.core.logger import get_logger
from app.models.heal_task import HealTask

logger = get_logger("services.heal_task_queue")


def create_task(
    session: Session,
    node_id: str,
    alert_rule_id: int,
    heal_rule_id: int,
    action_id: str,
    action_params: dict,
    risk_level: str,
    requires_approval: bool,
    verification_config: Optional[str] = None,
) -> HealTask:
    """创建自愈任务。"""
    task = HealTask(
        node_id=node_id,
        alert_rule_id=alert_rule_id,
        heal_rule_id=heal_rule_id,
        action_id=action_id,
        action_params=json.dumps(action_params, ensure_ascii=False),
        risk_level=risk_level,
        requires_approval=requires_approval,
        status="pending" if requires_approval else "approved",
        verification_config=verification_config,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def get_task_by_id(session: Session, task_id: int) -> Optional[HealTask]:
    """根据数据库 ID 获取任务。"""
    return session.get(HealTask, task_id)


def get_task_by_task_id(session: Session, task_id: str) -> Optional[HealTask]:
    """根据业务 task_id 获取任务。"""
    return session.exec(
        select(HealTask).where(HealTask.task_id == task_id)
    ).first()


def list_pending_tasks(
    session: Session,
    node_id: Optional[str] = None,
) -> list[HealTask]:
    """列出待确认任务。"""
    query = select(HealTask).where(HealTask.status == "pending").order_by(
        HealTask.created_at
    )
    if node_id is not None:
        query = query.where(HealTask.node_id == node_id)
    return list(session.exec(query).all())


def list_tasks_for_node(
    session: Session,
    node_id: str,
    status: Optional[str] = None,
) -> list[HealTask]:
    """列出分配给某节点的任务。"""
    query = select(HealTask).where(HealTask.node_id == node_id)
    if status is not None:
        query = query.where(HealTask.status == status)
    query = query.order_by(HealTask.created_at)
    return list(session.exec(query).all())


def approve_task(
    session: Session,
    task: HealTask,
    approved_by: int,
) -> HealTask:
    """将任务标记为已确认。"""
    if task.status != "pending":
        raise ValueError(f"任务状态不是 pending，当前: {task.status}")

    now = datetime.now(timezone.utc)
    task.status = "approved"
    task.approved_by = approved_by
    task.approved_at = now
    task.updated_at = now
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def start_task(
    session: Session,
    task: HealTask,
    executed_by: str,
) -> HealTask:
    """将任务标记为运行中。"""
    if task.status not in {"pending", "approved"}:
        raise ValueError(f"任务不能从 {task.status} 状态开始执行")

    now = datetime.now(timezone.utc)
    task.status = "running"
    task.executed_by = executed_by
    task.started_at = now
    task.updated_at = now
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def reject_task(
    session: Session,
    task: HealTask,
    rejected_by: int,
    reason: Optional[str] = None,
) -> HealTask:
    """拒绝任务，保留记录。"""
    if task.status != "pending":
        raise ValueError(f"任务状态不是 pending，当前: {task.status}")

    now = datetime.now(timezone.utc)
    task.status = "rejected"
    task.approved_by = rejected_by
    task.approved_at = now
    task.error_message = reason
    task.updated_at = now
    session.add(task)
    session.commit()
    session.refresh(task)

    logger.info(f"拒绝自愈任务: task_id={task.task_id}, reason={reason}")
    return task


def retry_task(session: Session, task: HealTask) -> HealTask:
    """重试失败/超时/待重试任务。"""
    is_scheduled_retry = task.status == "approved" and task.scheduled_at is not None
    if task.status not in {"failed", "timeout"} and not is_scheduled_retry:
        raise ValueError(f"任务状态不可重试，当前: {task.status}")

    task.status = "approved"
    task.error_message = None
    task.result = None
    task.started_at = None
    task.finished_at = None
    task.executed_by = None
    task.verification_status = None
    task.verification_result = None
    task.verification_due_at = None
    task.scheduled_at = None
    task.updated_at = datetime.now(timezone.utc)
    session.add(task)
    session.commit()
    session.refresh(task)

    logger.info(f"重试自愈任务: task_id={task.task_id}")
    return task


def mark_task_timeout(session: Session, task: HealTask) -> HealTask:
    """将运行中任务标记为超时。"""
    if task.status != "running":
        raise ValueError(f"任务状态不是 running，当前: {task.status}")

    now = datetime.now(timezone.utc)
    task.status = "timeout"
    task.finished_at = now
    task.updated_at = now
    task.error_message = "执行超时"
    session.add(task)
    session.commit()
    session.refresh(task)

    logger.info(f"自愈任务超时: task_id={task.task_id}")
    return task
