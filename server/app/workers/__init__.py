"""后台 Worker 注册与生命周期（Phase 3 Step 05）。

在 ``main.py`` lifespan 中，仅 leader（持有 SingleInstanceGuard）调用
``start_workers``：注册默认 handler 并启动 Redis 队列消费者线程。
降级模式（无 Redis）下 ``start_workers`` 为 no-op，业务走同步路径。
"""

from app.core.logger import get_logger
from app.core.queues import QUEUE_ALERT_DETECT, QUEUE_METRIC_WRITE, QUEUE_NOTIFY_SEND
from app.core.task_queue import get_task_queue

logger = get_logger("workers")


def register_default_workers(queue=None):
    """注册默认 handler 到任务队列，返回该队列实例。"""
    q = queue or get_task_queue()
    # 延迟导入，避免 worker → 业务服务 → task_queue 的循环依赖。
    from app.workers.detector_worker import process_detect
    from app.workers.metric_worker import process_metric_write
    from app.workers.notify_worker import process_notify

    q.register_handler(QUEUE_METRIC_WRITE, process_metric_write)
    q.register_handler(QUEUE_ALERT_DETECT, process_detect)
    q.register_handler(QUEUE_NOTIFY_SEND, process_notify)
    return q


def start_workers():
    """在 leader 上启动后台 worker；降级模式下不启动任何线程。"""
    q = get_task_queue()
    if not q.is_async():
        logger.info("任务队列未启用（同步降级模式），不启动后台 worker")
        return q
    register_default_workers(q)
    q.start()
    logger.info("后台 worker 已启动（metric/detector/notify）")
    return q


def stop_workers(timeout: float = 10.0) -> None:
    """优雅停止后台 worker。"""
    try:
        get_task_queue().stop(timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"停止后台 worker 失败: {exc}")
