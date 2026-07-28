"""数据库备份管理 API 路由。"""

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.api.deps import get_db, require_admin
from app.core.backup import (
    BACKUP_FILENAME_PATTERN,
    backup_database,
    is_restorable_backup,
    list_backups,
    restore_database_from_backup,
)
from app.core.config import settings
from app.core.database import engine as db_engine
from app.core.logger import get_logger
from app.models.user import User
from app.schemas.backup import BackupRead, BackupResponse
from app.services import audit_log

router = APIRouter(prefix="/api/v1/backups", tags=["backups"])

logger = get_logger("api.backup")


def _backup_dir() -> Path:
    """返回备份目录路径。"""
    return Path(settings.BACKUP_DIR)


def _to_backup_read(path: Path) -> BackupRead:
    """将备份文件路径转换为响应模型。"""
    stat = path.stat()
    created_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return BackupRead(
        filename=path.name,
        size_bytes=stat.st_size,
        created_at=created_at,
        verified=True,
        restorable=is_restorable_backup(path),
    )


def _resolve_backup_file(filename: str) -> Path:
    """解析并校验备份文件名，返回绝对路径。"""
    if BACKUP_FILENAME_PATTERN.match(filename) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="备份文件名格式不正确",
        )
    backup_path = _backup_dir() / filename
    backup_path = backup_path.resolve()
    # 防止路径遍历
    if backup_path.parent != _backup_dir().resolve():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法备份文件路径",
        )
    if not backup_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="备份文件不存在",
        )
    return backup_path


def _is_running_under_systemd() -> bool:
    """判断当前进程是否由 systemd 管理。"""
    # systemd 会为每个 service 设置 INVOCATION_ID，且 /run/systemd/system 存在
    if os.environ.get("INVOCATION_ID"):
        return True
    systemd_dir = Path("/run/systemd/system")
    return systemd_dir.exists() and systemd_dir.is_dir()


def _trigger_server_restart() -> None:
    """在 systemd 环境下异步触发 Server 重启。

    使用 subprocess.Popen 避免阻塞当前请求，给响应留出发送时间。
    """
    try:
        subprocess.Popen(
            ["systemctl", "restart", "chaosops-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("已触发 systemd 自动重启: systemctl restart chaosops-server")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"触发 systemd 自动重启失败: {exc}")


@router.get("", response_model=list[BackupRead])
def get_backups(
    current_user: User = Depends(require_admin),
):
    """列出所有备份文件（admin）。"""
    return [_to_backup_read(p) for p in list_backups()]


@router.post("", response_model=BackupRead, status_code=status.HTTP_201_CREATED)
def create_backup(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """手动触发数据库备份（admin）。"""
    backup_path = backup_database()
    audit_log.record_backup_created(db, backup_path.name, created_by=current_user.id)
    return _to_backup_read(backup_path)


@router.post("/{filename}/restore", response_model=BackupResponse)
def restore_backup(
    filename: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """从指定备份文件恢复数据库（admin）。

    恢复前会先对当前数据库做一次备份，防止恢复失败丢失数据。
    """
    backup_path = _resolve_backup_file(filename)

    # 1. 先对当前数据库做一次备份
    try:
        pre_backup_path = backup_database()
        logger.info(f"恢复前自动备份当前数据库: {pre_backup_path}")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"恢复前自动备份失败: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="恢复前自动备份失败",
        ) from exc

    # 2. 执行原子恢复：先关闭当前请求会话并释放引擎连接，
    # 避免覆盖后旧句柄仍指向已被替换的 inode。
    try:
        db.close()
        restore_database_from_backup(
            backup_path, dispose_callback=db_engine.dispose
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"数据库恢复失败: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="数据库恢复失败",
        ) from exc

    # 3. 替换成功之后，用全新会话把审计写入新库。
    # 关键：必须在新连接上写审计，否则会落到已被替换的旧库。
    with Session(db_engine) as audit_session:
        audit_log.record_backup_restored(
            audit_session, backup_path.name, created_by=current_user.id
        )

    if _is_running_under_systemd():
        _trigger_server_restart()
        return BackupResponse(
            message="数据库恢复完成，已触发 Server 自动重启",
            backup=_to_backup_read(pre_backup_path),
        )

    return BackupResponse(
        message="数据库恢复完成，请手动重启 Server 以使所有连接生效",
        backup=_to_backup_read(pre_backup_path),
    )


@router.delete("/{filename}", status_code=status.HTTP_204_NO_CONTENT)
def delete_backup(
    filename: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """删除指定备份文件（admin）。"""
    backup_path = _resolve_backup_file(filename)

    try:
        backup_path.unlink()
    except OSError as exc:
        logger.error(f"删除备份失败: {backup_path}, error={exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="删除备份失败",
        ) from exc

    audit_log.record_backup_deleted(db, backup_path.name, created_by=current_user.id)
    return None
