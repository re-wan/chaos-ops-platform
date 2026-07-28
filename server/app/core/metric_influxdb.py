"""InfluxDB 指标存储后端实现。"""

import re
from datetime import datetime, timezone
from typing import Optional

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.query_api import QueryApi
from influxdb_client.client.write_api import WriteOptions

from app.core.config import settings
from app.core.logger import get_logger
from app.core.metric_backend import MetricBackend
from app.models.metric import MetricSample

logger = get_logger("core.metric_influxdb")

# InfluxDB tag key 只支持部分字符，过滤非法标签名
_TAG_KEY_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# 删除 predicate 中 tag 值的白名单校验（防注入）：仅允许字母数字、点、下划线、中划线。
# node_id / metric_name 由平台生成或配置，合法值不会超出该字符集。
_DELETE_TAG_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _sanitize_tag_key(key: str) -> Optional[str]:
    """过滤非法 tag key，返回 None 表示丢弃。"""
    if not key or key.startswith("_"):
        return None
    if not _TAG_KEY_PATTERN.match(key):
        return None
    return key


def _to_point(sample: MetricSample) -> Point:
    """将 MetricSample 转换为 InfluxDB Point。

    Measurement: metric
    Tags: node_id, metric_name, 以及 sample.labels 中的合法标签
    Fields: value
    """
    timestamp = sample.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    point = (
        Point("metric")
        .tag("node_id", sample.labels.get("node_id") if sample.labels else "")
        .tag("metric_name", sample.metric_name)
        .field("value", float(sample.value))
        .time(timestamp)
    )

    if sample.labels:
        for key, val in sample.labels.items():
            clean_key = _sanitize_tag_key(key)
            if clean_key is None or clean_key in {"node_id", "metric_name"}:
                continue
            point = point.tag(clean_key, str(val))

    return point


class InfluxDBMetricBackend(MetricBackend):
    """基于 InfluxDB 的 MetricBackend 实现。"""

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        org: Optional[str] = None,
        bucket: Optional[str] = None,
        batch_size: int = 1000,
        flush_interval: int = 1000,
    ):
        self.url = url or settings.INFLUXDB_URL
        self.token = token or settings.INFLUXDB_TOKEN
        self.org = org or settings.INFLUXDB_ORG
        self.bucket = bucket or settings.INFLUXDB_BUCKET
        # 初始化时创建长期 InfluxDBClient，避免高并发下重复构造
        self._client = InfluxDBClient(
            url=self.url,
            token=self.token,
            org=self.org,
        )
        # 使用异步批处理写入选项，提升高并发写入吞吐
        self._write_api = self._client.write_api(
            write_options=WriteOptions(
                batch_size=batch_size,
                flush_interval=flush_interval,
                jitter_interval=0,
                retry_interval=5_000,
                max_retries=3,
                max_retry_delay=30_000,
                exponential_base=2,
            )
        )
        self._query_api: QueryApi = self._client.query_api()
        self._delete_api = self._client.delete_api()

    def _get_client(self) -> InfluxDBClient:
        """返回已初始化的长期 InfluxDBClient。"""
        return self._client

    def write(self, samples: list[MetricSample]) -> dict:
        """批量写入样本。"""
        if not samples:
            return {"accepted": 0, "dropped": 0}

        points = []
        dropped = 0
        for sample in samples:
            try:
                points.append(_to_point(sample))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"构造 Point 失败: {exc}")
                dropped += 1

        if not points:
            return {"accepted": 0, "dropped": dropped}

        try:
            self._get_client()
            self._write_api.write(bucket=self.bucket, record=points)
            return {"accepted": len(points), "dropped": dropped}
        except Exception as exc:  # noqa: BLE001
            logger.error(f"InfluxDB 批量写入失败: {exc}")
            return {"accepted": 0, "dropped": dropped + len(points)}

    def query(
        self,
        node_id: str,
        metric_name: str,
        start: datetime,
        end: datetime,
        labels: Optional[dict] = None,
    ) -> list[MetricSample]:
        """使用 Flux 查询样本。"""
        labels = labels or {}

        start_iso = start.isoformat()
        end_iso = end.isoformat()

        filters = [
            'r._measurement == "metric"',
            f'r.node_id == "{node_id}"',
            f'r.metric_name == "{metric_name}"',
            'r._field == "value"',
        ]
        for key, val in labels.items():
            clean_key = _sanitize_tag_key(key)
            if clean_key is None:
                continue
            filters.append(f'r.{clean_key} == "{val}"')

        filter_expr = " and ".join(filters)
        flux = f'''
        from(bucket: "{self.bucket}")
            |> range(start: {start_iso}, stop: {end_iso})
            |> filter(fn: (r) => {filter_expr})
        '''

        try:
            self._get_client()
            tables = self._query_api.query(flux, org=self.org)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"InfluxDB 查询失败: {exc}")
            return []

        results: list[MetricSample] = []
        for table in tables:
            for record in table.records:
                ts = record.get_time()
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                sample_labels = {"node_id": node_id}
                # 将查询过滤外的其他标签也收集进来
                for key, val in (record.values or {}).items():
                    if key.startswith("_") or key in {"result", "table"}:
                        continue
                    if isinstance(val, str):
                        sample_labels[key] = val

                results.append(
                    MetricSample(
                        metric_name=metric_name,
                        value=float(record.get_value()),
                        timestamp=ts,
                        labels=sample_labels,
                    )
                )
        return results

    def _count_metric_points(
        self,
        start: datetime,
        stop: datetime,
        node_id: Optional[str] = None,
        metric_name: Optional[str] = None,
    ) -> int:
        """统计 [start, stop) 范围内 metric 域（_measurement/_field）的样本条数。

        InfluxDB delete API 不返回受影响行数，故删除前用 ``|> count()`` 聚合，
        供调用方获得真实清理条数（best-effort：与删除之间存在微小竞态）。
        调用前必须保证 node_id / metric_name 已通过白名单校验。
        """
        filters = ['r._measurement == "metric"', 'r._field == "value"']
        if node_id is not None:
            filters.append(f'r.node_id == "{node_id}"')
        if metric_name is not None:
            filters.append(f'r.metric_name == "{metric_name}"')
        flux = f'''
        from(bucket: "{self.bucket}")
            |> range(start: {start.isoformat()}, stop: {stop.isoformat()})
            |> filter(fn: (r) => {" and ".join(filters)})
            |> count()
        '''
        tables = self._query_api.query(flux, org=self.org)
        total = 0
        for table in tables:
            for record in table.records:
                val = record.get_value()
                if val is not None:
                    total += int(val)
        return total

    def delete_before(
        self,
        timestamp: datetime,
        node_id: Optional[str] = None,
        metric_name: Optional[str] = None,
    ) -> int:
        """删除 timestamp 之前的 metric 域数据，返回清理条数（best-effort）。

        predicate 限定到 ``_measurement="metric" and _field="value"``，避免误删
        bucket 内其他 measurement/field（如运维监控数据）；node_id / metric_name
        提供时追加 tag 过滤（用于 per-指标保留期覆盖）。
        tag 值拼入 predicate 前做白名单字符校验，非法值跳过本次删除并告警。
        """
        predicate_parts = ['_measurement="metric"', '_field="value"']
        for tag_key, tag_val in (("node_id", node_id), ("metric_name", metric_name)):
            if tag_val is None:
                continue
            if not _DELETE_TAG_VALUE_PATTERN.match(tag_val):
                logger.warning(
                    f"InfluxDB 清理跳过: {tag_key} 含非法字符（{tag_val!r}），拒绝拼入 predicate"
                )
                return 0
            predicate_parts.append(f'{tag_key}="{tag_val}"')
        predicate = " and ".join(predicate_parts)

        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        start = datetime(1970, 1, 1, tzinfo=timezone.utc)
        stop = timestamp

        try:
            self._get_client()
            # 先统计目标域内真实条数，再按限定 predicate 删除
            deleted = self._count_metric_points(
                start, stop, node_id=node_id, metric_name=metric_name
            )
            self._delete_api.delete(
                start=start,
                stop=stop,
                bucket=self.bucket,
                org=self.org,
                predicate=predicate,
            )
            logger.info(
                f"InfluxDB 清理完成: {stop.isoformat()} 之前约 {deleted} 条 metric 数据"
            )
            return deleted
        except Exception as exc:  # noqa: BLE001
            logger.error(f"InfluxDB 清理失败: {exc}")
            return 0

    def check_health(self) -> bool:
        """Ping InfluxDB 检查健康状态，复用长期 Client。"""
        try:
            return self._client.ping()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"InfluxDB 健康检查失败: {exc}")
            return False

    def close(self) -> None:
        """关闭 InfluxDB 连接。"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"关闭 InfluxDB 客户端失败: {exc}")
            finally:
                self._client = None
                self._write_api = None
                self._query_api = None
                self._delete_api = None
