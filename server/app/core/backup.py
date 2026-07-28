"""数据库备份、恢复与保留策略工具。

面向 SQLite 的在线热备份，不中断 Server 服务。
"""

import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("core.backup")

BACKUP_FILENAME_PATTERN = re.compile(r"^chaosops_backup_(\d{8}_\d{6})(?:_\d+)?\.db$")
# PostgreSQL pg_dump 自定义格式备份（仅展示，不开放 API 恢复）
BACKUP_DUMP_FILENAME_PATTERN = re.compile(
    r"^chaosops_backup_(\d{8}_\d{6})(?:_\d+)?\.dump$"
)


class BackupBackendError(RuntimeError):
    """备份后端不支持或缺少必要工具（调用方可据此跳过而非告警）。"""
BACKUP_FILENAME_FORMAT = "chaosops_backup_%Y%m%d_%H%M%S.db"


def _detect_backend(database_url: Optional[str] = None) -> str:
    """根据 DATABASE_URL 判断数据库后端：sqlite / postgresql / unknown。"""
    url = (database_url or settings.DATABASE_URL).lower()
    if url.startswith("sqlite"):
        return "sqlite"
    if url.startswith("postgresql"):
        return "postgresql"
    return "unknown"


def get_database_file_path(database_url: Optional[str] = None) -> Path:
    """从 DATABASE_URL 解析 SQLite 数据库文件路径。"""
    url = database_url or settings.DATABASE_URL
    if not url.startswith("sqlite:///"):
        raise RuntimeError(f"当前 DATABASE_URL 不是 SQLite 路径: {url}")
    path_str = url[len("sqlite:///") :]
    return Path(path_str).resolve()


def _warn_if_insecure(path: Path) -> None:
    """备份文件权限/属主过宽时记录 warning（不阻断流程）。"""
    try:
        st = path.stat()
    except OSError:
        return
    mode = st.st_mode & 0o777
    if mode & 0o077:
        logger.warning(f"备份文件权限过宽（{oct(mode)}），建议设为 0600: {path}")
    try:
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            logger.warning(f"备份文件属主与当前进程不一致: {path}")
    except OSError:
        pass


def _ensure_dir(path: Path) -> None:
    """确保目录存在且可写。"""
    path.mkdir(parents=True, exist_ok=True)
    test_file = path / ".write_test"
    try:
        test_file.write_text("")
        test_file.unlink()
    except OSError as exc:
        raise RuntimeError(f"目录不可写: {path}, error={exc}") from exc


def _integrity_check(db_path: Path) -> bool:
    """对指定 SQLite 文件执行 PRAGMA integrity_check。"""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute("PRAGMA integrity_check")
            result = cur.fetchone()
            return result is not None and result[0] == "ok"
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"完整性检查失败: {db_path}, error={exc}")
        return False


def backup_database(
    source_path: Optional[str] = None,
    backup_dir: Optional[str] = None,
) -> Path:
    """按后端分派执行数据库备份。

    - SQLite：在线热备份（``.db``）。
    - PostgreSQL：调用 ``pg_dump``（``.dump``，密码走 PGPASSWORD 环境变量）。

    Args:
        source_path: 源数据库文件路径；显式提供时按 SQLite 文件处理（兼容旧调用）。
        backup_dir: 备份存放目录，默认使用 settings.BACKUP_DIR。

    Returns:
        生成的备份文件路径。

    Raises:
        BackupBackendError: 后端不支持或缺少工具（调用方可据此跳过而非告警）。
        RuntimeError: 备份或完整性校验失败。
    """
    # 显式 source_path 始终按 SQLite 文件处理
    if source_path is not None:
        return _backup_sqlite(Path(source_path), backup_dir)

    backend = _detect_backend()
    if backend == "sqlite":
        return _backup_sqlite(get_database_file_path(), backup_dir)
    if backend == "postgresql":
        return _backup_postgres(backup_dir)
    raise BackupBackendError(
        f"不支持的数据库备份后端: {settings.DATABASE_URL}（已跳过）"
    )


def _next_backup_path(target_dir: Path, stem_prefix: str, ext: str) -> Path:
    """生成不冲突的备份文件路径（同秒多次备份自动追加序号）。"""
    timestamp = datetime.now(timezone.utc)
    base = timestamp.strftime(f"{stem_prefix}_%Y%m%d_%H%M%S")
    backup_path = target_dir / f"{base}{ext}"
    if backup_path.exists():
        suffix = 1
        while backup_path.exists():
            backup_path = target_dir / f"{base}_{suffix}{ext}"
            suffix += 1
    return backup_path


def _backup_sqlite(db_path: Path, backup_dir: Optional[str] = None) -> Path:
    """执行 SQLite 在线热备份，生成的备份文件权限设为 0600。"""
    target_dir = Path(backup_dir) if backup_dir else Path(settings.BACKUP_DIR)
    _ensure_dir(target_dir)
    backup_path = _next_backup_path(target_dir, "chaosops_backup", ".db")

    if not db_path.exists():
        raise RuntimeError(f"源数据库文件不存在: {db_path}")

    source = sqlite3.connect(str(db_path))
    backup = sqlite3.connect(str(backup_path))
    try:
        with backup:
            source.backup(backup)
    finally:
        backup.close()
        source.close()

    if not _integrity_check(backup_path):
        try:
            backup_path.unlink()
        except OSError as exc:
            logger.warning(f"删除未通过完整性校验的备份失败: {backup_path}, error={exc}")
        raise RuntimeError(f"备份文件完整性校验失败: {backup_path}")

    os.chmod(backup_path, 0o600)
    logger.info(f"数据库备份完成: {backup_path}")
    return backup_path


def _backup_postgres(backup_dir: Optional[str] = None) -> Path:
    """执行 PostgreSQL 备份（pg_dump 自定义格式），密码走 PGPASSWORD 环境变量。

    缺 pg_dump 时抛出 BackupBackendError，由调用方跳过而非每天告警。
    """
    from urllib.parse import urlparse

    if shutil.which("pg_dump") is None:
        raise BackupBackendError("未找到 pg_dump，无法备份 PostgreSQL（已跳过）")

    target_dir = Path(backup_dir) if backup_dir else Path(settings.BACKUP_DIR)
    _ensure_dir(target_dir)
    url = urlparse(settings.DATABASE_URL)
    backup_path = _next_backup_path(target_dir, "chaosops_backup", ".dump")

    args = [
        "pg_dump",
        "-Fc",
        "-h",
        url.hostname or "localhost",
        "-p",
        str(url.port or 5432),
        "-U",
        url.username or "postgres",
        "-d",
        (url.path or "/").lstrip("/") or "postgres",
        "-f",
        str(backup_path),
    ]
    # 密码通过环境变量传入，绝不出现在命令行参数或日志中
    env = os.environ.copy()
    if url.password:
        env["PGPASSWORD"] = url.password
    logger.info(
        f"执行 PostgreSQL 备份: pg_dump -h {url.hostname} -p {url.port or 5432} "
        f"-U {url.username} -d {(url.path or '/').lstrip('/')}"
    )
    try:
        subprocess.run(
            args, env=env, check=True, capture_output=True, text=True, timeout=3600
        )
    except subprocess.CalledProcessError as exc:
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(f"pg_dump 备份失败: {stderr or exc}") from exc

    os.chmod(backup_path, 0o600)
    logger.info(f"PostgreSQL 备份完成: {backup_path}")
    return backup_path


def _parse_backup_datetime(filename: str) -> Optional[datetime]:
    """从备份文件名解析创建时间（兼容 .db 与 .dump）。"""
    match = BACKUP_FILENAME_PATTERN.match(
        filename
    ) or BACKUP_DUMP_FILENAME_PATTERN.match(filename)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def is_restorable_backup(path: Path) -> bool:
    """判断备份文件是否可通过 API 恢复。

    SQLite ``.db`` 可恢复；PostgreSQL ``.dump``（pg_dump 自定义格式）仅展示、
    不开放 API 恢复（需运维用 pg_restore 手工操作，避免误覆盖生产库）。
    """
    return BACKUP_FILENAME_PATTERN.match(path.name) is not None


def list_backups(backup_dir: Optional[str] = None) -> list[Path]:
    """列出所有备份文件，按时间从新到旧排序。

    同时展示 SQLite ``.db`` 与 PostgreSQL ``.dump``；其中 ``.dump`` 仅展示，
    调用方可通过 ``is_restorable_backup`` 判断是否可恢复（API 层据此标记
    ``restorable=false``，不开放恢复）。
    """
    target_dir = Path(backup_dir) if backup_dir else Path(settings.BACKUP_DIR)
    if not target_dir.exists():
        return []

    backups = []
    for path in target_dir.iterdir():
        if not path.is_file():
            continue
        if BACKUP_FILENAME_PATTERN.match(path.name) or BACKUP_DUMP_FILENAME_PATTERN.match(
            path.name
        ):
            backups.append(path)

    backups.sort(
        key=lambda p: (_parse_backup_datetime(p.name) or datetime.min),
        reverse=True,
    )
    return backups


def cleanup_old_backups(
    backup_dir: Optional[str] = None,
    keep_count: int = 7,
) -> list[Path]:
    """保留最近 keep_count 个备份，删除更旧的。

    Args:
        backup_dir: 备份目录，默认 settings.BACKUP_DIR。
        keep_count: 保留数量。

    Returns:
        被删除的备份文件路径列表。
    """
    backups = list_backups(backup_dir)
    if len(backups) <= keep_count:
        return []

    deleted: list[Path] = []
    for old in backups[keep_count:]:
        try:
            old.unlink()
            deleted.append(old)
            logger.info(f"删除旧备份: {old}")
        except OSError as exc:
            logger.warning(f"删除旧备份失败: {old}, error={exc}")

    return deleted


def restore_database_from_backup(
    backup_path: str | Path,
    target_path: Optional[str | Path] = None,
    dispose_callback: Optional[Callable[[], None]] = None,
) -> Path:
    """从备份文件原子恢复数据库。

    流程：释放目标库现有连接 -> 备份写到同目录临时文件 -> ``os.replace`` 原子替换。
    调用方应在替换成功**之后**再用新连接写审计（确保审计落到新库）。

    Args:
        backup_path: 备份文件路径。
        target_path: 目标数据库路径，默认从 settings.DATABASE_URL 解析。
        dispose_callback: 可选回调，用于在替换前释放目标数据库连接（如 engine.dispose）。

    Returns:
        恢复后的目标数据库路径。
    """
    backup_path = Path(backup_path)
    db_path = Path(target_path) if target_path else get_database_file_path()

    if not backup_path.exists():
        raise RuntimeError(f"备份文件不存在: {backup_path}")

    # 备份文件权限/属主过宽时告警（不阻断）
    _warn_if_insecure(backup_path)

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 1) 释放目标库现有连接，避免覆盖后旧句柄仍指向已替换的 inode
    if dispose_callback is not None:
        try:
            dispose_callback()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"释放目标库连接失败（继续恢复）: {exc}")

    # 2) 先写到同目录临时文件，再 os.replace 原子替换（同文件系统保证原子性）
    tmp_path = db_path.with_name(db_path.name + ".restore_tmp")
    try:
        shutil.copy2(backup_path, tmp_path)
        os.replace(tmp_path, db_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    logger.info(f"数据库已从备份原子恢复: {backup_path} -> {db_path}")
    return db_path
