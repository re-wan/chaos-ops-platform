"""通知渠道后台任务。

负责异步处理单条通知记录以及轮询到期重试。
"""

from app.core.scheduler import safe_task
from app.services.notification import process_notification_log, retry_pending_notifications


@safe_task
def process_notification_log_task(log_id: int) -> None:
    """异步处理单条通知记录。"""
    process_notification_log(log_id)


@safe_task
def retry_pending_notifications_task() -> None:
    """轮询并触发到期重试的通知记录。"""
    retry_pending_notifications()
