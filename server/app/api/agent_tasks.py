"""Agent 任务拉取与结果上报 API 路由。"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.api.deps import get_current_node, get_db
from app.models.heal_task import HealTask
from app.models.node import Node
from app.models.remote_execution import RemoteExecution
from app.schemas.heal_task import HealTaskResultUpdate
from app.services import heal_action as heal_action_service
from app.services import heal_executor, heal_task_queue

try:
    from app.services import remote_execution as remote_execution_service
except ImportError:
    # 三版物理分包删除远程执行服务后，跳过远程任务下发，普通自愈任务不受影响。
    remote_execution_service = None


router = APIRouter(prefix="/api/v1/agent/tasks", tags=["agent-tasks"])


def _task_to_agent_payload(session: Session, task: HealTask) -> dict:
    """将自愈任务转换为 Agent 可执行的 payload。"""
    payload = {
        "task_type": "heal",
        "task_id": task.task_id,
        "action_id": task.action_id,
        "action_params": json.loads(task.action_params),
        "risk_level": task.risk_level,
        "timeout_seconds": heal_executor.get_execution_timeout_seconds(),
    }

    # 脚本类动作（custom_script / ai_generated_script）需要从 HealAction 实时查询内容并注入
    action = heal_action_service.get_action_by_action_id(session, task.action_id)
    if action is not None and action.action_type in heal_action_service.SCRIPT_ACTION_TYPES:
        payload["action_type"] = action.action_type
        payload["script_content"] = action.script_content
        payload["script_hash"] = action.script_hash
        payload["interpreter"] = action.interpreter

    return payload


def _remote_execution_to_agent_payload(execution: RemoteExecution) -> dict:
    """将远程执行记录转换为 Agent 可执行的 payload。"""
    return {
        "task_type": "remote",
        "task_id": execution.execution_id,
        "action_id": execution.action_id,
        "action_params": json.loads(execution.action_params),
        "timeout_seconds": execution.timeout_seconds,
    }


def _mark_task_dispatch_failed(
    session: Session, task: HealTask, reason: str
) -> HealTask:
    """任务无法下发时标记为失败。"""
    now = datetime.now(timezone.utc)
    task.status = "failed"
    task.error_message = reason
    task.finished_at = now
    task.updated_at = now
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


@router.get("/pending")
def get_pending_tasks(
    db: Session = Depends(get_db),
    current_node: Node = Depends(get_current_node),
):
    """Agent 拉取分配给自己的待执行任务。"""
    tasks = heal_task_queue.list_tasks_for_node(
        db, current_node.node_id, status="running"
    )

    payloads = []
    for task in tasks:
        action = heal_action_service.get_action_by_action_id(db, task.action_id)
        if action is None:
            _mark_task_dispatch_failed(
                db, task, f"动作不存在: {task.action_id}"
            )
            continue

        if action.action_type in heal_action_service.SCRIPT_ACTION_TYPES:
            # 实时校验：未审批或 hash 不匹配的任务不下发
            if not action.is_approved:
                _mark_task_dispatch_failed(
                    db, task, f"脚本未审批: {task.action_id}"
                )
                continue
            if not heal_action_service.verify_script_hash(action):
                _mark_task_dispatch_failed(
                    db, task, f"脚本 hash 校验失败: {task.action_id}"
                )
                continue

        payloads.append(_task_to_agent_payload(db, task))

    # 同时拉取 pending 状态的远程执行任务（模块被物理删除时跳过）
    if remote_execution_service is not None:
        remote_executions = remote_execution_service.get_pending_remote_executions_for_node(
            db, current_node.node_id
        )
        for execution in remote_executions:
            remote_execution_service.start_remote_execution(db, execution)
            payloads.append(_remote_execution_to_agent_payload(execution))

    return {
        "node_id": current_node.node_id,
        "tasks": payloads,
    }


@router.post("/{task_id}/result")
def report_task_result(
    task_id: str,
    body: HealTaskResultUpdate,
    db: Session = Depends(get_db),
    current_node: Node = Depends(get_current_node),
):
    """Agent 上报任务执行结果。"""
    # remote 类型任务以 re_ 开头（模块被物理删除时落入下方 heal 分支并 404）
    if remote_execution_service is not None and task_id.startswith("re_"):
        execution = remote_execution_service.get_remote_execution(db, task_id)
        if execution is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="执行记录不存在",
            )
        if execution.node_id != current_node.node_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权上报其他节点的执行结果",
            )

        remote_execution_service.record_remote_execution_result(
            db,
            execution_id=task_id,
            success=body.success,
            output=body.output,
            error_message=body.error_message or body.message,
        )
        return {"task_id": task_id, "status": execution.status}

    # heal 类型任务
    task = heal_task_queue.get_task_by_task_id(db, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="任务不存在",
        )

    if task.node_id != current_node.node_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权上报其他节点的任务",
        )

    if task.status != "running":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"任务状态不是 running，当前: {task.status}",
        )

    heal_executor.record_task_result(
        db,
        task,
        success=body.success,
        output=body.output,
        message=body.message,
        error_message=body.error_message,
    )

    return {"task_id": task_id, "status": task.status}
