"""指标历史数据迁移服务。

支持 InfluxDB 与 SQLite 之间的双向迁移，按样本指纹去重，
保证幂等性：重复执行不会导致数据重复。
"""

from datetime import datetime, timezone
from typing import Iterator, Optional

from app.core.logger import get_logger
from app.core.metric_backend import MetricBackend
from app.core.metric_influxdb import InfluxDBMetricBackend
from app.core.metric_sqlite import SQLiteMetricBackend
from app.models.metric import MetricSample

logger = get_logger("services.metric_migration")

DEFAULT_BATCH_SIZE = 1000


def _iter_influxdb_samples(
    backend: InfluxDBMetricBackend,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> Iterator[MetricSample]:
    """从 InfluxDB 后端流式读取所有样本。

    默认读取 1970 年至今的全部数据，可按时间范围缩小。
    """
    start = start or datetime(1970, 1, 1, tzinfo=timezone.utc)
    end = end or datetime.now(timezone.utc)

    flux = f'''
    from(bucket: "{backend.bucket}")
        |> range(start: {start.isoformat()}, stop: {end.isoformat()})
        |> filter(fn: (r) => r._measurement == "metric" and r._field == "value")
    '''

    try:
        tables = backend._query_api.query(flux, org=backend.org)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"从 InfluxDB 读取样本失败: {exc}")
        return

    for table in tables:
        for record in table.records:
            ts = record.get_time()
            if ts is None:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            node_id = ""
            labels = {}
            for key, val in (record.values or {}).items():
                if key.startswith("_") or key in {"result", "table"}:
                    continue
                if isinstance(val, str):
                    labels[key] = val
                    if key == "node_id":
                        node_id = val

            metric_name = labels.get("metric_name", "")
            value = float(record.get_value())

            yield MetricSample(
                metric_name=metric_name,
                value=value,
                timestamp=ts,
                labels={"node_id": node_id, **labels},
            )


def _iter_sqlite_samples(
    backend: SQLiteMetricBackend,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> Iterator[MetricSample]:
    """从 SQLite 后端流式读取所有样本。"""
    yield from backend.iter_all_samples(start=start, end=end)


def _make_sample_key(sample: MetricSample) -> tuple:
    """生成样本指纹，用于去重。

    使用 node_id + metric_name + 时间戳 + value，
    value 保留 10 位小数避免浮点误差。
    """
    labels = sample.labels or {}
    node_id = labels.get("node_id", "")
    ts = sample.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (
        node_id,
        sample.metric_name,
        ts.isoformat(),
        round(float(sample.value), 10),
    )


def migrate_between_backends(
    source_type: str,
    target_type: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    source_config: Optional[dict] = None,
    target_config: Optional[dict] = None,
) -> int:
    """在两种指标后端之间迁移历史数据。

    Args:
        source_type: 源后端类型："influxdb" 或 "sqlite"。
        target_type: 目标后端类型："influxdb" 或 "sqlite"。
        start: 可选起始时间。
        end: 可选结束时间。
        batch_size: 批量写入大小。
        source_config: 源后端可选配置覆盖。
        target_config: 目标后端可选配置覆盖。

    Returns:
        实际写入目标后端的样本数。
    """
    source_config = source_config or {}
    target_config = target_config or {}

    source_backend: MetricBackend
    target_backend: MetricBackend

    if source_type == "influxdb":
        source_backend = InfluxDBMetricBackend(
            url=source_config.get("influxdb_url"),
            token=source_config.get("influxdb_token"),
            org=source_config.get("influxdb_org"),
            bucket=source_config.get("influxdb_bucket"),
        )
    elif source_type == "sqlite":
        source_backend = SQLiteMetricBackend(
            database_url=source_config.get("metric_sqlite_database_url")
        )
    else:
        raise ValueError(f"非法源后端类型: {source_type}")

    if target_type == "influxdb":
        target_backend = InfluxDBMetricBackend(
            url=target_config.get("influxdb_url"),
            token=target_config.get("influxdb_token"),
            org=target_config.get("influxdb_org"),
            bucket=target_config.get("influxdb_bucket"),
        )
    elif target_type == "sqlite":
        target_backend = SQLiteMetricBackend(
            database_url=target_config.get("metric_sqlite_database_url")
        )
    else:
        raise ValueError(f"非法目标后端类型: {target_type}")

    try:
        # 校验两端连接
        if not source_backend.check_health():
            raise RuntimeError(f"源后端 {source_type} 连接不可用")
        if not target_backend.check_health():
            raise RuntimeError(f"目标后端 {target_type} 连接不可用")

        # 预扫描目标后端已有数据，生成指纹集合，保证跨多次迁移幂等
        seen: set = set()
        if isinstance(target_backend, SQLiteMetricBackend):
            for sample in target_backend.iter_all_samples():
                seen.add(_make_sample_key(sample))
        elif isinstance(target_backend, InfluxDBMetricBackend):
            for sample in _iter_influxdb_samples(target_backend):
                seen.add(_make_sample_key(sample))

        if source_type == "influxdb":
            iterator = _iter_influxdb_samples(source_backend, start=start, end=end)
        else:
            iterator = _iter_sqlite_samples(source_backend, start=start, end=end)

        batch: list[MetricSample] = []
        migrated_count = 0
        total_read = 0

        for sample in iterator:
            total_read += 1
            key = _make_sample_key(sample)
            if key in seen:
                continue
            seen.add(key)
            batch.append(sample)

            if len(batch) >= batch_size:
                result = target_backend.write(batch)
                migrated_count += result.get("accepted", 0)
                batch = []

        if batch:
            result = target_backend.write(batch)
            migrated_count += result.get("accepted", 0)

        logger.info(
            f"指标迁移完成: {source_type} -> {target_type}, "
            f"读取 {total_read} 条，写入 {migrated_count} 条"
        )
        return migrated_count
    finally:
        try:
            source_backend.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"关闭源后端失败: {exc}")
        try:
            target_backend.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"关闭目标后端失败: {exc}")
