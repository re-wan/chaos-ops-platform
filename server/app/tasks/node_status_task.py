"""节点状态检查任务。"""

from sqlmodel import Session

from app.core.database import engine
from app.core.logger import get_logger
from app.services.node_service import check_and_mark_offline_nodes

logger = get_logger("tasks.node_status")


def check_offline_nodes_task() -> int:
    """定期检查并标记超时未心跳节点为离线。"""
    try:
        with Session(engine) as session:
            marked = check_and_mark_offline_nodes(session)
            if marked > 0:
                logger.info(f"标记 {marked} 个节点为离线")
            return marked
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"节点离线检查任务失败: {exc}")
        return 0
