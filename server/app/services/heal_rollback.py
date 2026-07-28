"""自愈回滚逻辑占位模块。

Phase 1（MVP）仅保留接口与日志记录，不实现自动回滚。
Phase 2 将引入执行前快照、回滚动作与二次确认机制。
"""

from sqlmodel import Session

from app.core.logger import get_logger
from app.models.heal_task import HealTask

logger = get_logger("services.heal_rollback")


def can_rollback(task: HealTask) -> bool:
    """判断任务是否具备回滚条件。MVP 阶段始终返回 False。"""
    return False


def rollback_task(session: Session, task: HealTask) -> HealTask:
    """触发回滚。MVP 阶段仅记录日志并抛出异常。

    Args:
        session: 数据库会话。
        task: 需要回滚的任务。

    Raises:
        NotImplementedError: 自动回滚在 Phase 1 未实现。
    """
    logger.warning(
        f"自动回滚未实现: task_id={task.task_id}, "
        f"action_id={task.action_id}"
    )
    raise NotImplementedError("自动回滚在 Phase 1 未实现，请人工处理")
