"""SQLite 指标存储后端实现。

作为 InfluxDB 的降级后端，适用于小规模部署或 PoC 场景。
数据存储在 SQLite 的 `metric_samples` 表中，查询通过复合索引加速。
"""

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from app.core.config import settings
from app.core import database as database_module
from app.core.logger import get_logger
from app.core.metric_backend import MetricBackend
from app.core.utils import ensure_utc
from app.models.metric import MetricSample
from app.models.metric_sqlite import SQLiteMetricSample

logger = get_logger("core.metric_sqlite")

# 分批删除批大小：SQLite 不支持 DELETE ... LIMIT，通过 rowid 子查询分批，
# 避免千万级数据单条 DELETE 长事务锁住采集写入热点库。
_DELETE_BATCH_SIZE = 10_000


class SQLiteMetricBackend(MetricBackend):
    """基于 SQLite 的 MetricBackend 实现。"""

    def __init__(self, database_url: Optional[str] = None):
        self._database_url = database_url or settings.METRIC_SQLITE_DATABASE_URL
        if self._database_url:
            # 使用独立引擎（可指向单独文件），并关闭同线程检查以兼容多线程
            connect_args = (
                {"check_same_thread": False}
                if self._database_url.startswith("sqlite")
                else {}
            )
            self._engine = create_engine(
                self._database_url,
                connect_args=connect_args,
            )
            self._using_shared_engine = False
        else:
            # 默认复用关系型数据库引擎
            self._engine = database_module.engine
            self._using_shared_engine = True

    def _ensure_table(self) -> None:
        """确保表与索引存在。

        create_all 默认 checkfirst=True，重复调用不会重建已有表，
        因此共享引擎和独立引擎下都可以安全调用。
        """
        SQLModel.metadata.create_all(self._engine, tables=[SQLiteMetricSample.__table__])

    def _now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def _encode_labels(self, labels: Optional[dict]) -> Optional[str]:
        if not labels:
            return None
        # 排除已存储为独立字段的 node_id，减少冗余
        cleaned = {k: v for k, v in labels.items() if k != "node_id"}
        return json.dumps(cleaned, sort_keys=True) if cleaned else None

    def _decode_labels(self, labels_json: Optional[str], node_id: str) -> dict:
        labels: dict = {"node_id": node_id}
        if labels_json:
            try:
                labels.update(json.loads(labels_json))
            except json.JSONDecodeError as exc:
                logger.warning(f"解析 labels_json 失败: {exc}")
        return labels

    def _normalize_timestamp(self, ts: datetime) -> datetime:
        """将时间戳统一转换为 naive UTC datetime，便于 SQLite 存储与比较。

        SQLite 没有原生时区支持，统一按 UTC 存储可保证查询与删除的正确性。
        """
        if ts.tzinfo is not None:
            return ts.astimezone(timezone.utc).replace(tzinfo=None)
        return ts

    def write(self, samples: list[MetricSample]) -> dict:
        """批量写入样本。"""
        if not samples:
            return {"accepted": 0, "dropped": 0}

        self._ensure_table()

        rows = []
        dropped = 0
        for sample in samples:
            try:
                labels = dict(sample.labels) if sample.labels else {}
                node_id = labels.get("node_id", "")
                rows.append(
                    SQLiteMetricSample(
                        node_id=node_id,
                        metric_name=sample.metric_name,
                        value=float(sample.value),
                        timestamp=self._normalize_timestamp(sample.timestamp),
                        labels_json=self._encode_labels(labels),
                        created_at=self._now_utc(),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"构造 SQLite 样本失败: {exc}")
                dropped += 1

        if not rows:
            return {"accepted": 0, "dropped": dropped}

        try:
            with Session(self._engine) as session:
                session.add_all(rows)
                session.commit()
            return {"accepted": len(rows), "dropped": dropped}
        except Exception as exc:  # noqa: BLE001
            logger.error(f"SQLite 批量写入失败: {exc}")
            return {"accepted": 0, "dropped": dropped + len(rows)}

    def query(
        self,
        node_id: str,
        metric_name: str,
        start: datetime,
        end: datetime,
        labels: Optional[dict] = None,
    ) -> list[MetricSample]:
        """按节点、指标名、时间范围查询样本。"""
        labels = labels or {}
        start = self._normalize_timestamp(start)
        end = self._normalize_timestamp(end)

        try:
            with Session(self._engine) as session:
                statement = (
                    select(SQLiteMetricSample)
                    .where(SQLiteMetricSample.node_id == node_id)
                    .where(SQLiteMetricSample.metric_name == metric_name)
                    .where(SQLiteMetricSample.timestamp >= start)
                    .where(SQLiteMetricSample.timestamp <= end)
                    .order_by(SQLiteMetricSample.timestamp)
                )
                rows = session.exec(statement).all()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"SQLite 查询失败: {exc}")
            return []

        results: list[MetricSample] = []
        for row in rows:
            row_labels = self._decode_labels(row.labels_json, row.node_id)
            # 在内存中按 labels 二次过滤（labels_json 不走索引，但主过滤已用索引缩小范围）
            if labels and not all(row_labels.get(k) == v for k, v in labels.items()):
                continue

            ts = ensure_utc(row.timestamp)
            results.append(
                MetricSample(
                    metric_name=row.metric_name,
                    value=row.value,
                    timestamp=ts,
                    labels=row_labels,
                )
            )
        return results

    def delete_before(
        self,
        timestamp: datetime,
        node_id: Optional[str] = None,
        metric_name: Optional[str] = None,
    ) -> int:
        """删除 timestamp 之前的数据，返回删除条数。

        node_id / metric_name 可选：提供时按 (node_id, metric_name) 过滤，
        走 idx_metric_samples_lookup 复合索引，用于 per-指标保留期覆盖。

        统一走分批删除（rowid 子查询 + 逐批 commit）：避免 7475 万行级数据
        单条 DELETE 长事务锁住采集写入热点库；按时间条件删除天然幂等，
        分批中断后下次任务可续删。
        """
        timestamp = self._normalize_timestamp(timestamp)
        try:
            self._ensure_table()
            where_clauses = ["timestamp < :ts"]
            params: dict = {"ts": timestamp, "batch": _DELETE_BATCH_SIZE}
            if node_id is not None:
                where_clauses.append("node_id = :node_id")
                params["node_id"] = node_id
            if metric_name is not None:
                where_clauses.append("metric_name = :metric_name")
                params["metric_name"] = metric_name
            where_sql = " AND ".join(where_clauses)

            total = 0
            with Session(self._engine) as session:
                while True:
                    statement = text(
                        "DELETE FROM metric_samples WHERE rowid IN "
                        f"(SELECT rowid FROM metric_samples WHERE {where_sql} LIMIT :batch)"
                    ).bindparams(**params)
                    result = session.exec(statement)
                    session.commit()
                    count = result.rowcount if hasattr(result, "rowcount") else 0
                    if count <= 0:
                        break
                    total += count
            return total
        except Exception as exc:  # noqa: BLE001
            logger.error(f"SQLite 清理失败: {exc}")
            return 0

    def check_health(self) -> bool:
        """检查 SQLite 连接是否可用。"""
        try:
            if not self._using_shared_engine:
                self._ensure_table()
            with Session(self._engine) as session:
                session.exec(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SQLite 健康检查失败: {exc}")
            return False

    def close(self) -> None:
        """释放 SQLite 引擎连接。

        共享引擎由应用生命周期统一管理，独立引擎才需要关闭。
        """
        if not self._using_shared_engine and self._engine is not None:
            try:
                self._engine.dispose()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"关闭 SQLite 引擎失败: {exc}")
            finally:
                self._engine = None

    def iter_all_samples(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        batch_size: int = 1000,
    ):
        """全表流式迭代样本，供迁移工具使用。

        按主键分页读取，避免一次性加载全表。
        """
        self._ensure_table()
        last_id: Optional[int] = None
        while True:
            with Session(self._engine) as session:
                statement = select(SQLiteMetricSample)
                if start is not None:
                    statement = statement.where(SQLiteMetricSample.timestamp >= start)
                if end is not None:
                    statement = statement.where(SQLiteMetricSample.timestamp <= end)
                if last_id is not None:
                    statement = statement.where(SQLiteMetricSample.id > last_id)
                statement = statement.order_by(SQLiteMetricSample.id).limit(batch_size)
                rows = list(session.exec(statement).all())
                if not rows:
                    break
                for row in rows:
                    yield MetricSample(
                        metric_name=row.metric_name,
                        value=row.value,
                        timestamp=ensure_utc(row.timestamp),
                        labels=self._decode_labels(row.labels_json, row.node_id),
                    )
                last_id = rows[-1].id
