#!/usr/bin/env python3
"""指标历史数据迁移命令行脚本。

用法：
    python backend/scripts/migrate_metrics.py --source influxdb --target sqlite
    python backend/scripts/migrate_metrics.py --source sqlite --target influxdb
    python backend/scripts/migrate_metrics.py --source influxdb --target sqlite \
        --start 2026-01-01T00:00:00Z --end 2026-02-01T00:00:00Z
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# 将 backend 目录加入模块搜索路径
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.metric_migration import migrate_between_backends


def parse_iso_timestamp(value: str) -> datetime:
    """解析 ISO 格式时间戳，缺省时区视为 UTC。"""
    # Python 3.11+ 支持 fromisoformat 解析带 Z 后缀的时间
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在 InfluxDB 与 SQLite 指标后端之间迁移历史数据"
    )
    parser.add_argument(
        "--source",
        required=True,
        choices=["influxdb", "sqlite"],
        help="源后端类型",
    )
    parser.add_argument(
        "--target",
        required=True,
        choices=["influxdb", "sqlite"],
        help="目标后端类型",
    )
    parser.add_argument(
        "--start",
        type=parse_iso_timestamp,
        default=None,
        help="起始时间（ISO 8601，可选）",
    )
    parser.add_argument(
        "--end",
        type=parse_iso_timestamp,
        default=None,
        help="结束时间（ISO 8601，可选）",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="批量写入大小（默认 1000）",
    )
    args = parser.parse_args()

    print(f"开始迁移: {args.source} -> {args.target}")
    try:
        count = migrate_between_backends(
            source_type=args.source,
            target_type=args.target,
            start=args.start,
            end=args.end,
            batch_size=args.batch_size,
        )
        print(f"迁移完成，共写入 {count} 条样本")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"迁移失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
