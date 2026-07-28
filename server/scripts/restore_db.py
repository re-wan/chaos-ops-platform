#!/usr/bin/env python3
"""命令行数据库恢复脚本。

用法：
    python backend/scripts/restore_db.py \
        --backup /opt/chaosops/backups/chaosops_backup_20260707_020000.db

恢复前会先对当前数据库做一次备份。
"""

import argparse
import sys
from pathlib import Path

# 允许从 backend 目录导入 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.backup import backup_database, restore_database_from_backup
from app.core.logger import get_logger, setup_logging

logger = get_logger("scripts.restore_db")


def main() -> int:
    """命令行入口。"""
    setup_logging()

    parser = argparse.ArgumentParser(description="恢复 ChaosOps SQLite 数据库")
    parser.add_argument(
        "--backup",
        required=True,
        help="备份文件路径",
    )
    args = parser.parse_args()

    backup_path = Path(args.backup).resolve()
    if not backup_path.exists():
        logger.error(f"备份文件不存在: {backup_path}")
        return 1

    try:
        pre_backup = backup_database()
        logger.info(f"恢复前自动备份当前数据库: {pre_backup}")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"恢复前自动备份失败: {exc}")
        return 1

    try:
        restore_database_from_backup(backup_path)
        logger.info(f"数据库已从备份恢复: {backup_path}")
        logger.info("建议重启 ChaosOps Server 以使所有数据库连接生效")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"数据库恢复失败: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
