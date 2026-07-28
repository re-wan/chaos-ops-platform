#!/usr/bin/env python3
"""SQLite → PostgreSQL 迁移工具。

用法：
    python backend/scripts/migrate_to_postgres.py \\
        --source sqlite:///./chaosops.db \\
        --target postgresql://user:pass@localhost/chaosops \\
        [--dry-run]

说明：
- 默认源数据库由环境变量/配置文件决定；可通过 --source 覆盖。
- 迁移前会自动备份源 SQLite 数据库到 BACKUP_DIR。
- 按表间外键依赖顺序逐表拷贝数据。
- 迁移失败时会尝试清理目标库中已创建的表。
- --dry-run 只打印迁移计划，不写入目标库。
"""

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# 将 backend 加入路径，以便导入 app 模块
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import Table, create_engine, make_url, select, text
from sqlalchemy.engine import Engine

from sqlmodel import SQLModel

from app.core.config import settings


# 单批读取/写入行数上限：避免 .all() 把整表加载进内存。
MIGRATE_BATCH_SIZE = 5000


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="将 ChaosOps SQLite 数据库迁移到 PostgreSQL"
    )
    parser.add_argument(
        "--source",
        default=settings.DATABASE_URL,
        help="源 SQLite 数据库 URL（默认使用 settings.DATABASE_URL）",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="目标 PostgreSQL 数据库 URL",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印迁移计划，不执行实际迁移",
    )
    parser.add_argument(
        "--target-password-env",
        default="CHAOSOPS_TARGET_DATABASE_PASSWORD",
        help="从环境变量读取目标 PostgreSQL 密码（默认 CHAOSOPS_TARGET_DATABASE_PASSWORD）",
    )
    return parser.parse_args()


def sqlite_path_from_url(source_url: str) -> Path:
    """从 SQLite URL 中提取数据库文件路径。"""
    url = make_url(source_url)
    db_path_str = url.database
    if not db_path_str or db_path_str == ":memory:":
        raise ValueError(f"无法从 SQLite URL 中提取数据库文件路径: {source_url}")
    # url.database 已包含前导斜杠（绝对路径）或相对路径
    return Path(db_path_str).resolve()


def backup_sqlite(source_url: str) -> Path:
    """备份 SQLite 数据库文件到 BACKUP_DIR。"""
    if not source_url.startswith("sqlite"):
        raise ValueError("--source 必须是 SQLite URL")

    db_path = sqlite_path_from_url(source_url)
    if not db_path.exists():
        raise FileNotFoundError(f"源数据库文件不存在: {db_path}")

    backup_dir = Path(settings.BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"pre_postgres_migration_{timestamp}_{db_path.name}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def create_target_tables(target_engine: Engine, dry_run: bool) -> None:
    """在目标 PostgreSQL 中创建所有表。"""
    # 导入模型确保 metadata 已注册
    from app import models  # noqa: F401

    if dry_run:
        print("[dry-run] 将执行 SQLModel.metadata.create_all(target_engine)")
        return

    SQLModel.metadata.create_all(target_engine)


def reset_postgres_sequences(target_engine: Engine, tables: list[Table]) -> None:
    """重置 PostgreSQL 自增序列，使其从当前最大 id 继续。

    通过 ``pg_get_serial_sequence(table, col)`` 取真实序列名（不再硬编码
    ``<table>_<col>_seq``），取不到时记录 warning 而非静默跳过，便于发现表结构异常。
    """
    preparer = target_engine.dialect.identifier_preparer
    with target_engine.connect() as conn:
        for table in tables:
            pk_columns = list(table.primary_key.columns)
            if not pk_columns:
                continue
            column = pk_columns[0]
            seq_name = conn.execute(
                text("SELECT pg_get_serial_sequence(:table, :col)"),
                {"table": table.name, "col": column.name},
            ).scalar()
            if not seq_name:
                print(
                    f"  [警告] 表 {table.name} 列 {column.name} 无 serial 序列，跳过重置"
                )
                continue
            quoted_table = preparer.quote(table.name)
            quoted_column = preparer.quote(column.name)
            conn.execute(
                text(
                    f"SELECT setval(:seq, COALESCE((SELECT MAX({quoted_column}) "
                    f"FROM {quoted_table}), 1))"
                ),
                {"seq": seq_name},
            )
        conn.commit()


def _iter_table_batches(source_conn, table: Table, batch_size: int):
    """按 PK 游标分批读取源表行，避免整表 .all()。

    - 有主键：使用 ``WHERE pk > last ORDER BY pk LIMIT batch`` 键集分页，稳定且无 OFFSET 退化。
    - 无主键：退化为 LIMIT/OFFSET（项目内业务表均为单列整型主键，此分支为兜底）。
    """
    pk_columns = list(table.primary_key.columns)
    if pk_columns:
        pk = pk_columns[0]
        last_value = None
        while True:
            stmt = select(table).order_by(pk).limit(batch_size)
            if last_value is not None:
                stmt = stmt.where(pk > last_value)
            batch = source_conn.execute(stmt).mappings().all()
            if not batch:
                break
            yield batch
            last_value = batch[-1][pk.name]
            if len(batch) < batch_size:
                break
        return

    offset = 0
    while True:
        stmt = select(table).limit(batch_size).offset(offset)
        batch = source_conn.execute(stmt).mappings().all()
        if not batch:
            break
        yield batch
        offset += len(batch)
        if len(batch) < batch_size:
            break


def migrate_data(source_engine: Engine, target_engine: Engine, dry_run: bool) -> dict:
    """按外键依赖顺序逐表拷贝数据，并返回各表迁移记录数。

    流式分批：每批最多 MIGRATE_BATCH_SIZE 行，边读边写，避免整表加载进内存。
    失败回滚语义不变：所有写入仍在单一目标事务中，异常时目标事务不提交，
    由调用方触发 cleanup_target_tables 清理已建表。
    """
    # sorted_tables 已经按外键依赖拓扑排序
    tables = SQLModel.metadata.sorted_tables
    stats = {}

    if dry_run:
        print("[dry-run] 计划拷贝以下表（已按外键依赖排序）：")
        for table in tables:
            print(f"  - {table.name}")
        return stats

    with source_engine.connect() as source_conn:
        with target_engine.connect() as target_conn:
            for table in tables:
                total = 0
                for batch in _iter_table_batches(
                    source_conn, table, MIGRATE_BATCH_SIZE
                ):
                    values = [dict(row) for row in batch]
                    # 批量插入（目标为全新建表，主键冲突理论上不会发生）
                    target_conn.execute(table.insert(), values)
                    total += len(values)

                stats[table.name] = total
                if total == 0:
                    print(f"  {table.name}: 0 条记录，跳过")
                else:
                    print(f"  {table.name}: 迁移 {total} 条记录")

            target_conn.commit()

    reset_postgres_sequences(target_engine, tables)
    return stats


def verify_counts(source_engine: Engine, target_engine: Engine) -> bool:
    """验证源库与目标库各表记录数量一致。"""
    ok = True
    with source_engine.connect() as source_conn:
        with target_engine.connect() as target_conn:
            for table in SQLModel.metadata.sorted_tables:
                source_count = source_conn.execute(
                    text(f"SELECT COUNT(*) FROM {table.name}")
                ).scalar()
                target_count = target_conn.execute(
                    text(f"SELECT COUNT(*) FROM {table.name}")
                ).scalar()
                if source_count != target_count:
                    print(
                        f"  [不一致] {table.name}: 源 {source_count} ≠ 目标 {target_count}"
                    )
                    ok = False
                else:
                    print(f"  [一致] {table.name}: {source_count} 条")
    return ok


def cleanup_target_tables(target_engine: Engine) -> None:
    """迁移失败时清理目标库中已创建的表（按依赖逆序删除）。"""
    with target_engine.connect() as conn:
        # 逆序删除，先删有外键依赖的子表
        for table in reversed(SQLModel.metadata.sorted_tables):
            try:
                conn.execute(text(f"DROP TABLE IF EXISTS {table.name} CASCADE"))
                print(f"  已清理目标表: {table.name}")
            except Exception as e:  # noqa: BLE001
                print(f"  清理目标表 {table.name} 失败: {e}")
        conn.commit()


def normalize_sqlite_url(source_url: str) -> str:
    """规范化 SQLite URL，支持常见的 sqlite:///absolute/path 写法。

    SQLAlchemy 要求绝对路径使用 4 个斜杠（sqlite:////abs/path），但用户习惯写成
    3 个斜杠（sqlite:///abs/path）。本函数自动检测并将后者转换为前者。
    """
    three_slash_prefix = "sqlite:///"
    four_slash_prefix = "sqlite:////"
    if not source_url.startswith(three_slash_prefix) or source_url.startswith(four_slash_prefix):
        return source_url
    remainder = source_url[len(three_slash_prefix) :]
    # 相对路径前缀直接放行
    if remainder.startswith("./") or remainder.startswith("../"):
        return source_url
    # 若 /remainder 存在文件，则按绝对路径处理
    absolute_candidate = "/" + remainder
    if Path(absolute_candidate).exists():
        return four_slash_prefix + remainder
    return source_url


def inject_password_from_env(target_url: str, env_var: str) -> str:
    """如果目标 URL 密码为空，从环境变量读取并注入。"""
    url = make_url(target_url)
    if url.password:
        return target_url
    password = os.environ.get(env_var, "").strip()
    if not password:
        return target_url
    # 构造带密码的新 URL；注意 str(URL) 会脱敏密码，必须使用 render_as_string
    new_url = url.set(password=password)
    return new_url.render_as_string(hide_password=False)


def main() -> int:
    """迁移工具入口。"""
    args = parse_args()

    if not args.source.startswith("sqlite"):
        print("错误: --source 必须是 SQLite URL")
        return 1
    if not args.target.startswith("postgresql"):
        print("错误: --target 必须是 PostgreSQL URL")
        return 1

    args.source = normalize_sqlite_url(args.source)
    args.target = inject_password_from_env(args.target, args.target_password_env)

    print(f"源数据库: {args.source}")
    print(f"目标数据库: {args.target.replace(args.target.split('@')[0].split(':')[-1], '***') if '@' in args.target else args.target}")
    if args.dry_run:
        print("模式: dry-run（不写入目标库）")

    # 1. 备份源数据库
    if not args.dry_run:
        try:
            backup_path = backup_sqlite(args.source)
            print(f"源数据库已备份到: {backup_path}")
        except Exception as e:  # noqa: BLE001
            print(f"备份源数据库失败: {e}")
            return 1

    # 2. 创建引擎
    source_engine = create_engine(args.source)
    target_engine = create_engine(
        args.target,
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_MAX_OVERFLOW,
        pool_recycle=settings.DATABASE_POOL_RECYCLE_SECONDS,
    )

    try:
        # 3. 在目标库建表
        print("正在目标库创建表...")
        create_target_tables(target_engine, args.dry_run)

        # 4. 拷贝数据
        print("正在拷贝数据...")
        stats = migrate_data(source_engine, target_engine, args.dry_run)

        if args.dry_run:
            print("[dry-run] 迁移计划预览完成")
            return 0

        # 5. 验证数量
        print("正在验证记录数量...")
        if not verify_counts(source_engine, target_engine):
            print("迁移失败: 记录数量不一致")
            cleanup_target_tables(target_engine)
            return 1

        total = sum(stats.values())
        print(f"迁移成功: 共 {len(stats)} 张表，{total} 条记录")
        return 0

    except Exception as e:  # noqa: BLE001
        print(f"迁移过程中出错: {e}")
        if not args.dry_run:
            print("正在清理目标库...")
            cleanup_target_tables(target_engine)
        return 1

    finally:
        source_engine.dispose()
        target_engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
