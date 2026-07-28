"""通知发送 Worker（Phase 3 Step 05）。

消费 ``notify.send`` 队列：按 ``log_id`` 投递一条 pending 通知记录。实际发送、
失败重试（指数退避）、成功/失败状态更新复用 ``notification.process_notification_log``，
本 worker 只负责把"立即投递"从 APScheduler 迁移到 Redis 队列，便于多 worker 水平扩展。

幂等性：process_notification_log 以 log_id 为幂等键，重复投递不会重复发送
（状态非 pending 时直接返回）。
"""

from app.core.logger import get_logger

logger = get_logger("workers.notify")


def process_notify(payload: dict) -> None:
    """处理一个 notify.send 任务：投递指定 NotificationLog。"""
    log_id = payload.get("log_id")
    if log_id is None:
        logger.warning(f"notify.send 任务缺少 log_id，跳过: {payload!r}")
        return

    from app.services.notification import process_notification_log

    process_notification_log(int(log_id))
