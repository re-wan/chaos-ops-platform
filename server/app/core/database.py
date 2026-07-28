"""ChaosOps Server 数据库连接与初始化。

支持 SQLite（默认，适合本地开发与轻量部署）和 PostgreSQL（生产推荐）。
通过 ``DATABASE_URL`` 前缀自动选择引擎类型；PostgreSQL 使用可配置连接池。
"""

import os
import time
from contextlib import contextmanager
from typing import Generator, Iterator, Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.core import single_instance
from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("core.database")


def _setup_slow_query_logging(engine: Engine) -> None:
    """为引擎安装慢查询日志监听器（Phase 3 Step 05）。

    单次 SQL 执行耗时超过 ``settings.SLOW_QUERY_THRESHOLD_MS`` 时记录 warning，
    便于定位大时间范围/未命中索引的查询。SQLite 与 PostgreSQL 双后端通用。
    """
    if not settings.SLOW_QUERY_LOG_ENABLED:
        return
    threshold_ms = float(settings.SLOW_QUERY_THRESHOLD_MS)

    @event.listens_for(engine, "before_cursor_execute")
    def _before_execute(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault("_cq_start", []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _after_execute(conn, cursor, statement, parameters, context, executemany):
        stack = conn.info.get("_cq_start")
        start = stack.pop() if stack else None
        if start is None:
            return
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if elapsed_ms >= threshold_ms:
            logger.warning(
                "慢查询 %.1fms >= %.1fms: %s",
                elapsed_ms,
                threshold_ms,
                _truncate_sql(statement),
            )


def _truncate_sql(statement: str, limit: int = 500) -> str:
    """截断并压缩 SQL 文本用于日志，避免泄露过长内容/刷屏。"""
    text = " ".join(str(statement).split())
    return text if len(text) <= limit else text[:limit] + "..."


def create_engine_with_url(database_url: str) -> Engine:
    """根据数据库 URL 创建合适的 SQLAlchemy 引擎。

    - SQLite：关闭同线程检查，以便 FastAPI 依赖注入正常工作。
      不接 pool_size/max_overflow：SQLite 单写者，默认池足够，调大收益有限。
    - PostgreSQL：使用连接池，大小由配置项控制（默认 20+20，按 worker 数×并发调整）。
    - 两者均安装慢查询日志监听器（受 SLOW_QUERY_LOG_ENABLED 控制）。
    """
    if database_url.startswith("postgresql"):
        engine = create_engine(
            database_url,
            pool_size=settings.DATABASE_POOL_SIZE,
            max_overflow=settings.DATABASE_MAX_OVERFLOW,
            pool_recycle=settings.DATABASE_POOL_RECYCLE_SECONDS,
        )
        _setup_slow_query_logging(engine)
        return engine

    connect_args = {"check_same_thread": False}
    engine = create_engine(database_url, connect_args=connect_args)
    _setup_slow_query_logging(engine)
    return engine


def _resolve_default_database_url() -> str:
    """解析 DATABASE_URL：V2 安装器通过 .env 传入绝对路径；本兜底用于兼容未设置场景。

    若 DATA_DIR 环境变量存在且当前使用默认相对 SQLite 路径（sqlite:///./chaosops.db），
    则将数据库文件定位到 DATA_DIR/chaosops.db，避免数据文件散落在 server 工作目录。
    """
    database_url = settings.DATABASE_URL
    data_dir = os.environ.get("DATA_DIR")
    if data_dir and database_url == "sqlite:///./chaosops.db":
        return f"sqlite:///{data_dir}/chaosops.db"
    return database_url


engine = create_engine_with_url(_resolve_default_database_url())

# init_db 进程间互斥锁文件名（位于 single_instance 默认锁目录下）
_DB_INIT_LOCK_NAME = "db_init.lock"


@contextmanager
def _db_init_lock() -> Iterator[None]:
    """init_db 的阻塞式进程间互斥锁。

    与 ``SingleInstanceGuard`` 的非阻塞"抢不到就跳过"不同：这里必须**等待**
    首个进程把表建完。多 worker（``uvicorn --workers N``）首次部署全新 DB 时，
    N 个进程并发执行 ``create_all``，SQLAlchemy 的 checkfirst 在多进程下非原子，
    会撞出 ``table ... already exists`` 使全部 worker 启动失败。持锁后执行
    ``create_all``；后拿到锁的进程因 checkfirst 发现表已存在，自然全部跳过、秒级返回。

    退化策略（fail-safe，与 single_instance 一致，宁运行勿误杀）：
    - 无 fcntl（非 Linux）或取锁异常 → warning 后直接执行 create_all。

    锁随文件描述符关闭自动释放，进程崩溃也不会残留死锁。
    """
    if not single_instance._HAS_FCNTL:
        logger.warning("当前平台无 fcntl，init_db 降级为无锁直接执行 create_all")
        yield
        return

    fd: Optional[int] = None
    try:
        lock_dir = single_instance._default_lock_dir()
        lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_dir / _DB_INIT_LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
        single_instance.fcntl.flock(fd, single_instance.fcntl.LOCK_EX)  # 阻塞等待
    except OSError as exc:
        # 机制异常：降级为无锁执行，宁可承担并发风险也不阻断启动
        logger.warning(
            f"init_db 取锁异常，降级为无锁直接执行 create_all（宁运行勿误杀）: {exc}"
        )
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)  # 锁随 fd 释放
            except OSError:
                pass


# nodes.name 部分唯一索引名（与 app/models/node.py 的 __table_args__ 保持一致）
_NODES_NAME_PARTIAL_INDEX = "uq_nodes_name_active"
# 旧的 name 全表唯一索引名（Field(index=True, unique=True) 生成）
_NODES_NAME_LEGACY_INDEX = "ix_nodes_name"


def _migrate_nodes_name_index() -> None:
    """幂等迁移：将 nodes.name 的全表唯一索引替换为排除软删行的部分唯一索引。

    背景：``SQLModel.metadata.create_all`` 不会修改已存在表的索引，因此存量库上
    旧的 ``UNIQUE INDEX ix_nodes_name`` 会一直存在，软删除节点后同名重建会触发
    IntegrityError。这里在每次启动时检查并修复：

    - 已存在部分唯一索引 → 已是最新，跳过（幂等）；
    - 否则 DROP 旧唯一索引 → 重建同名普通索引（按名称查询/筛选使用）→
      创建部分唯一索引（仅约束未软删除的行）。

    失败不阻断启动（宁运行勿误杀）：旧索引残留只会回到原有行为，不会丢数据。
    """
    try:
        with engine.begin() as conn:
            dialect = conn.dialect.name
            if dialect == "sqlite":
                exists = conn.exec_driver_sql(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name='"
                    + _NODES_NAME_PARTIAL_INDEX + "'"
                ).scalar()
                if exists:
                    return
                conn.exec_driver_sql(f"DROP INDEX IF EXISTS {_NODES_NAME_LEGACY_INDEX}")
                conn.exec_driver_sql(
                    f"CREATE INDEX IF NOT EXISTS {_NODES_NAME_LEGACY_INDEX} ON nodes (name)"
                )
                conn.exec_driver_sql(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {_NODES_NAME_PARTIAL_INDEX} "
                    "ON nodes (name) WHERE is_deleted = 0"
                )
            elif dialect == "postgresql":
                exists = conn.exec_driver_sql(
                    "SELECT 1 FROM pg_indexes WHERE indexname='"
                    + _NODES_NAME_PARTIAL_INDEX + "'"
                ).scalar()
                if exists:
                    return
                conn.exec_driver_sql(f"DROP INDEX IF EXISTS {_NODES_NAME_LEGACY_INDEX}")
                conn.exec_driver_sql(
                    f"CREATE INDEX IF NOT EXISTS {_NODES_NAME_LEGACY_INDEX} ON nodes (name)"
                )
                conn.exec_driver_sql(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {_NODES_NAME_PARTIAL_INDEX} "
                    "ON nodes (name) WHERE is_deleted = false"
                )
            else:
                return
            logger.info("nodes.name 索引迁移完成：全表唯一 → 部分唯一（排除软删除行）")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"nodes.name 索引迁移失败，保留旧索引（不影响启动）: {exc}")


# ai_config 表新增列的幂等迁移清单：列名 -> DDL 片段（SQLite/PostgreSQL 通用）
_AI_CONFIG_NEW_COLUMNS: dict[str, str] = {
    "auto_analyze": "BOOLEAN NOT NULL DEFAULT TRUE",
    "rate_mode": "VARCHAR NOT NULL DEFAULT 'adaptive'",
    "fixed_rate": "INTEGER NOT NULL DEFAULT 2",
    "max_rate": "INTEGER NOT NULL DEFAULT 10",
}


def _migrate_ai_config_columns() -> None:
    """幂等迁移：为存量 ai_config 表补充自动分析/限流相关新列。

    背景：``SQLModel.metadata.create_all`` 不会为已存在的表加列。新部署由
    create_all 直接建出全列表；存量库在这里逐列检查并 ``ALTER TABLE ADD COLUMN``
    （带默认值，老行自动取默认）。失败不阻断启动（宁运行勿误杀）：缺失列只会
    让自动分析功能读写报错并在日志中可见，不影响已有功能。
    """
    try:
        with engine.begin() as conn:
            dialect = conn.dialect.name
            if dialect == "sqlite":
                rows = conn.exec_driver_sql("PRAGMA table_info(ai_config)").all()
                existing = {row[1] for row in rows}
            elif dialect == "postgresql":
                rows = conn.exec_driver_sql(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='ai_config'"
                ).all()
                existing = {row[0] for row in rows}
            else:
                return
            if not existing:
                # 表不存在（全新库，create_all 随后会建全列表）
                return
            missing = [
                name for name in _AI_CONFIG_NEW_COLUMNS if name not in existing
            ]
            for name in missing:
                ddl = _AI_CONFIG_NEW_COLUMNS[name]
                conn.exec_driver_sql(
                    f"ALTER TABLE ai_config ADD COLUMN {name} {ddl}"
                )
            if missing:
                logger.info(f"ai_config 列迁移完成，新增列: {missing}")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"ai_config 列迁移失败，保留旧结构（不影响启动）: {exc}")


def _migrate_notification_channel_lang() -> None:
    """幂等迁移：为存量 notification_channels 表补充 lang 列。

    背景同 ``_migrate_ai_config_columns``：create_all 不为已存在表加列。
    存量行自动取默认值 'zh'，保持既有中文通知行为不变。
    失败不阻断启动（宁运行勿误杀）。
    """
    try:
        with engine.begin() as conn:
            dialect = conn.dialect.name
            if dialect == "sqlite":
                rows = conn.exec_driver_sql(
                    "PRAGMA table_info(notification_channels)"
                ).all()
                existing = {row[1] for row in rows}
            elif dialect == "postgresql":
                rows = conn.exec_driver_sql(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='notification_channels'"
                ).all()
                existing = {row[0] for row in rows}
            else:
                return
            if not existing or "lang" in existing:
                # 表不存在（全新库，create_all 会建全列表）或已是最新
                return
            conn.exec_driver_sql(
                "ALTER TABLE notification_channels "
                "ADD COLUMN lang VARCHAR NOT NULL DEFAULT 'zh'"
            )
            logger.info("notification_channels 列迁移完成，新增列: ['lang']")
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            f"notification_channels 列迁移失败，保留旧结构（不影响启动）: {exc}"
        )


def init_db() -> None:
    """初始化数据库：创建所有 SQLModel 定义的表。

    由 main.py 的 lifespan 在应用启动时调用。多 worker 首次部署全新 DB 时经
    阻塞式文件锁串行化（见 ``_db_init_lock``），避免并发 create_all 竞态。
    """
    with _db_init_lock():
        SQLModel.metadata.create_all(engine)
        _migrate_nodes_name_index()
        _migrate_ai_config_columns()
        _migrate_notification_channel_lang()


def get_session() -> Generator[Session, None, None]:
    """FastAPI 依赖：为每个请求生成一个数据库 Session。"""
    with Session(engine) as session:
        yield session
