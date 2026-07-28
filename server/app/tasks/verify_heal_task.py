"""自愈效果验证调度任务。

轮询待验证任务与待重试任务，调用验证与重试逻辑。
"""

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from sqlalchemy import Engine

from app.core.database import engine as default_engine
from app.core.logger import get_logger
from app.models.heal_task import HealTask
from app.services import heal_executor, heal_task_queue, heal_verification

logger = get_logger("tasks.verify_heal_task")


def _get_pending_verifications(session: Session, now: datetime) -> list[HealTask]:
    """查询已到验证时间的任务。"""
    return list(
        session.exec(
            select(HealTask).where(
                HealTask.verification_status == "pending",
                HealTask.verification_due_at <= now,
            )
        ).all()
    )


def _get_scheduled_retries(session: Session, now: datetime) -> list[HealTask]:
    """查询到达重试时间的任务。"""
    return list(
        session.exec(
            select(HealTask).where(
                HealTask.status == "approved",
                HealTask.scheduled_at <= now,
            )
        ).all()
    )


def _get_stuck_running_tasks(session: Session, now: datetime) -> list[HealTask]:
    """查询运行中且已卡死超时的任务。

    判定阈值：``started_at < now - (执行超时 + 清扫宽限)``。叠加宽限是为了
    避免误清扫刚启动、仍在正常执行窗口内的任务。
    """
    timeout = heal_executor.get_execution_timeout_seconds()
    grace = heal_executor.get_running_sweep_grace_seconds()
    cutoff = now - timedelta(seconds=timeout + grace)
    return list(
        session.exec(
            select(HealTask).where(
                HealTask.status == "running",
                HealTask.started_at.is_not(None),
                HealTask.started_at < cutoff,
            )
        ).all()
    )


def run_verification_checks(engine: Engine | None = None) -> None:
    """执行一轮验证检查与重试下发。

    Args:
        engine: 可选数据库引擎，用于测试注入。

    单个任务失败不影响其他任务。
    """
    now = datetime.now(timezone.utc)
    engine = engine or default_engine

    with Session(engine) as session:
        # 1. 执行到期的验证
        pending_tasks = _get_pending_verifications(session, now)
        for task in pending_tasks:
            try:
                heal_verification.verify_task(session, task)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    f"验证任务失败: task_id={task.task_id}, error={exc}"
                )

        # 2. 下发到期的重试任务
        retry_tasks = _get_scheduled_retries(session, now)
        for task in retry_tasks:
            try:
                heal_executor._dispatch_task(session, task, executed_by="auto")
                logger.info(
                    f"重试任务已下发: task_id={task.task_id}, "
                    f"retry_count={task.retry_count}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    f"重试任务下发失败: task_id={task.task_id}, error={exc}"
                )

        # 3. 清扫运行中超时卡死的任务（Agent 拉取后崩溃/断网导致 running 永驻，
        # 会永久阻塞同一 (node, action) 的后续自愈）。复用 mark_task_timeout
        # 走现有重试策略：可重试则回 approved 等待重新下发，达上限则 failed。
        stuck_tasks = _get_stuck_running_tasks(session, now)
        for task in stuck_tasks:
            try:
                heal_task_queue.mark_task_timeout(session, task)
                heal_verification.handle_execution_timeout(session, task)
                logger.info(
                    f"卡死 running 任务已清扫: task_id={task.task_id}, "
                    f"retry_count={task.retry_count}, status={task.status}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    f"卡死任务清扫失败: task_id={task.task_id}, error={exc}"
                )
