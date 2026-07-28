"""数据库定时备份任务。"""

from sqlmodel import Session

from app.core.backup import BackupBackendError, backup_database, cleanup_old_backups
from app.core.database import engine
from app.core.logger import get_logger
from app.core.scheduler import safe_task
from app.services import notification as notification_service

logger = get_logger("tasks.backup")


@safe_task
def backup_database_task() -> None:
    """定时执行数据库备份并清理旧备份。

    备份失败时记录 error 日志，并尝试发送通知告警。
    """
    logger.info("开始执行定时数据库备份任务")
    try:
        backup_path = backup_database()
        logger.info(f"定时数据库备份完成: {backup_path}")

        deleted = cleanup_old_backups()
        if deleted:
            logger.info(f"清理旧备份: {len(deleted)} 个")
    except BackupBackendError as exc:
        # 后端不支持或缺工具（如 PostgreSQL 但无 pg_dump）：跳过而非每天抛异常/告警
        logger.warning(f"跳过定时数据库备份（后端不可用）: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"定时数据库备份失败: {exc}")
        _notify_backup_failure(str(exc))


def _notify_backup_failure(error_message: str) -> None:
    """尝试通知管理员备份失败。"""
    try:
        with Session(engine) as session:
            notification_service.send_notification(
                session,
                event_type="system",
                event_id="backup_failed",
                payload={
                    "title": "数据库备份失败",
                    "message": f"定时数据库备份任务失败: {error_message}",
                    "severity": "critical",
                },
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"备份失败通知发送失败: {exc}")
