"""Server 自身韧性工具：启动自检、数据库备份与恢复。"""

import os
import shutil
import socket
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlmodel import Session

from app.core.config import settings
from app.core.database import engine
from app.core.logger import get_logger

logger = get_logger("resilience")


class StartupCheckError(Exception):
    """启动自检失败异常。"""

    pass


# InfluxDB token 占位值：安装脚本生成的 .env 会写入真实随机 token；若仍为占位值，
# 说明部署者未正确初始化 InfluxDB，按 fail-closed 拒绝启动。
INFLUXDB_TOKEN_PLACEHOLDER = "change-influxdb-token-in-production"


def is_placeholder_influxdb_token(token: Optional[str]) -> bool:
    """判断 InfluxDB token 是否为未替换的占位值。

    空值、等于已知占位串、或任何 ``change-`` 前缀均视为占位（fail-closed）。
    """
    if not token:
        return True
    token = token.strip()
    if token == INFLUXDB_TOKEN_PLACEHOLDER:
        return True
    if token.lower().startswith("change-"):
        return True
    return False


def check_port_available(host: str, port: int) -> bool:
    """检查指定端口是否可用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def get_database_file_path() -> Optional[Path]:
    """从 DATABASE_URL 解析 SQLite 数据库文件路径。"""
    url = settings.DATABASE_URL
    if not url.startswith("sqlite:///"):
        return None
    # sqlite:///./chaosops.db -> ./chaosops.db
    # sqlite:///absolute/path -> /absolute/path
    path_str = url[len("sqlite:///") :]
    return Path(path_str).resolve()


def _write_test_file(dir_path: Path) -> Path:
    """在目录中创建并删除一个进程唯一的写测试文件，返回使用的路径。

    文件名带 pid：多 worker 并发自检时若共用同一路径，会互相删除对方文件，
    FileNotFoundError 被误判为“目录不可写”导致 uvicorn master 整体停止。
    删除异常容错：写入已成功即说明目录可写，清理失败不影响判定。
    """
    test_file = dir_path / f".write_test.{os.getpid()}"
    test_file.write_text("")
    try:
        test_file.unlink()
    except OSError:
        pass
    return test_file


def _log_cookie_security_guidance() -> None:
    """COOKIE_SECURE=true 时打一条引导 INFO（H5，不阻断启动）。

    背景：HTTP 部署下 Secure Cookie 会被浏览器拒存，表现为“登录成功但立即被踢”。
    安装脚本默认按部署形态生成 COOKIE_SECURE；这里仅做运行期引导提醒。
    """
    if settings.COOKIE_SECURE:
        logger.info(
            "Secure Cookie 已启用（COOKIE_SECURE=true）：请确保经 TLS 反向代理访问；"
            "纯 HTTP 部署浏览器会拒存 Cookie 导致登录后立即被踢，此时应改为 false"
        )


def run_startup_checks() -> None:
    """Server 启动自检。

    任何一项失败都会抛出 StartupCheckError，阻止 Server 启动。
    """
    errors = []

    # 1. SECRET_KEY 必须配置且长度不少于 32 字节
    if not settings.SECRET_KEY or settings.SECRET_KEY == "change-me-in-production-please" or len(settings.SECRET_KEY) < 32:
        errors.append(
            "SECRET_KEY 未配置、使用默认值或长度不足。请设置长度不少于 32 字节的随机字符串，"
            "例如通过环境变量 SECRET_KEY 或 .env 文件配置。安装脚本会自动生成。"
        )

    # 1b. 显式配置的默认管理员口令若为弱口令，记强警告（不阻断启动，保持兼容）
    from app.core.security import is_weak_admin_password

    if settings.DEFAULT_ADMIN_PASSWORD and is_weak_admin_password(
        settings.DEFAULT_ADMIN_PASSWORD
    ):
        logger.warning(
            "安全警告：DEFAULT_ADMIN_PASSWORD 为弱口令，任何猜到默认值的人都可登录管理员账号；"
            "请立即修改为高强度随机口令，或删除该配置由 init_admin.py 随机生成。"
        )

    # 1c. COOKIE_SECURE 引导（H5）：true 时提醒必须经 TLS 反代访问（仅 INFO，不阻断）
    _log_cookie_security_guidance()

    # 2. 数据库文件/目录可读写（仅 SQLite）
    db_path = get_database_file_path()
    if db_path is not None:
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # 尝试创建/打开并写入
            with open(db_path, "a"):
                pass
            os.access(db_path, os.W_OK)
        except OSError as e:
            errors.append(f"数据库文件不可读写: {e}")

    # 3. 数据库连接正常 + 表已创建
    try:
        with Session(engine) as session:
            session.exec(text("SELECT 1"))
    except Exception as e:
        errors.append(f"数据库连接异常: {e}")

    # 4. 日志目录可写
    try:
        log_dir = Path(settings.LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        _write_test_file(log_dir)
    except OSError as e:
        errors.append(f"日志目录不可写: {e}")

    # 5. 备份目录可写
    try:
        backup_dir = Path(settings.BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)
        _write_test_file(backup_dir)
    except OSError as e:
        errors.append(f"备份目录不可写: {e}")

    # 6. 监听端口未被占用（仅检查默认端口 8000）
    if not check_port_available("0.0.0.0", 8000):
        errors.append("端口 8000 已被占用")

    # 6b. InfluxDB 占位 token 拒绝启动（仅当指标后端为 influxdb 时）
    try:
        from app.core.system_settings import get_metric_backend_type

        if (
            get_metric_backend_type() == "influxdb"
            and is_placeholder_influxdb_token(settings.INFLUXDB_TOKEN)
        ):
            errors.append(
                "INFLUXDB_TOKEN 仍为占位值（change-...），已拒绝启动。"
                "请通过 install-server.sh 重新初始化 InfluxDB，或手动设置真实 token；"
                "如不使用 InfluxDB，请将 METRIC_BACKEND_TYPE 设为 sqlite。"
            )
    except Exception as e:
        errors.append(f"INFLUXDB_TOKEN 校验异常: {e}")

    # 7. 指标后端健康检查
    if settings.METRIC_BACKEND_HEALTH_CHECK_AT_STARTUP:
        try:
            from app.services.metrics_ingest import get_metric_backend
            from app.core.system_settings import get_metric_backend_type

            backend = get_metric_backend()
            if not backend.check_health():
                backend_type = get_metric_backend_type()
                errors.append(
                    f"指标后端 {backend_type} 连接失败，请检查服务与配置"
                )
        except Exception as e:
            errors.append(f"指标后端健康检查异常: {e}")

    if errors:
        raise StartupCheckError("; ".join(errors))


def check_database_integrity() -> bool:
    """执行 SQLite PRAGMA integrity_check。"""
    db_path = get_database_file_path()
    if db_path is None:
        return True

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute("PRAGMA integrity_check")
        result = cur.fetchone()
        conn.close()
        return result is not None and result[0] == "ok"
    except Exception as e:
        logger.error(f"数据库完整性检查失败: {e}")
        return False


def backup_database() -> Path:
    """使用 SQLite 在线备份机制备份数据库。

    备份文件路径：BACKUP_DIR/chaosops_YYYYmmdd_HHMMSS.db
    保留最近 BACKUP_RETENTION_COUNT 个备份。
    """
    db_path = get_database_file_path()
    if db_path is None:
        raise RuntimeError("当前 DATABASE_URL 不是 SQLite，无法备份")

    backup_dir = Path(settings.BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = backup_dir / f"chaosops_{timestamp}.db"

    source = sqlite3.connect(str(db_path))
    backup = sqlite3.connect(str(backup_path))
    try:
        with backup:
            source.backup(backup)
    finally:
        backup.close()
        source.close()

    logger.info(f"数据库备份完成: {backup_path}")

    # 清理旧备份
    _cleanup_old_backups(backup_dir)

    return backup_path


def _cleanup_old_backups(backup_dir: Path) -> None:
    """保留最新的 N 个备份，删除旧备份。"""
    backups = sorted(backup_dir.glob("chaosops_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[settings.BACKUP_RETENTION_COUNT :]:
        try:
            old.unlink()
            logger.info(f"删除旧备份: {old}")
        except OSError as e:
            logger.warning(f"删除旧备份失败 {old}: {e}")


def restore_database_from_backup(backup_path: Path) -> None:
    """从备份文件恢复数据库。"""
    db_path = get_database_file_path()
    if db_path is None:
        raise RuntimeError("当前 DATABASE_URL 不是 SQLite，无法恢复")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, db_path)
    logger.info(f"数据库已从备份恢复: {backup_path} -> {db_path}")


def ensure_database_integrity() -> None:
    """启动时确保数据库完整。

    如果完整性检查失败，尝试从最新备份恢复；恢复失败则抛出异常。
    """
    if check_database_integrity():
        return

    logger.warning("数据库完整性检查失败，尝试从最新备份恢复")
    backup_dir = Path(settings.BACKUP_DIR)
    backups = sorted(backup_dir.glob("chaosops_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)

    if not backups:
        raise StartupCheckError("数据库损坏且没有可用备份")

    latest_backup = backups[0]
    try:
        restore_database_from_backup(latest_backup)
    except Exception as e:
        raise StartupCheckError(f"从备份恢复数据库失败: {e}")

    if not check_database_integrity():
        raise StartupCheckError("从备份恢复后数据库仍不完整")

    logger.info("数据库从备份恢复成功")
