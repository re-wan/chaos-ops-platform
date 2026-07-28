"""指标查询服务。

提供查询解析、step 选择、聚合函数与结果格式化。
"""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.logger import get_logger
from app.core.utils import ensure_utc
from app.core.metric_backend import MetricBackend
from app.models.metric import MetricSample
from app.services.metrics_ingest import query_metrics

logger = get_logger("services.metric_query")

_MAX_QUERY_RANGE_DAYS = 30
_MAX_POINTS = 5000

# step 字符串解析：支持 1m / 5m / 1h / 1d / 30s
_STEP_PATTERN = re.compile(r"^(\d+)([smhd])$")


def parse_step(step: str) -> timedelta:
    """将 step 字符串解析为 timedelta。

    Raises:
        ValueError: step 格式非法。
    """
    if not step:
        raise ValueError("step 不能为空")
    match = _STEP_PATTERN.match(step.strip())
    if not match:
        raise ValueError("step 格式非法，示例：1m / 5m / 1h / 1d")

    value = int(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return timedelta(seconds=value * multipliers[unit])


def format_step(step_delta: timedelta) -> str:
    """将 timedelta 还原为 step 字符串。"""
    total_seconds = int(step_delta.total_seconds())
    if total_seconds % 86400 == 0:
        return f"{total_seconds // 86400}d"
    if total_seconds % 3600 == 0:
        return f"{total_seconds // 3600}h"
    if total_seconds % 60 == 0:
        return f"{total_seconds // 60}m"
    return f"{total_seconds}s"


def parse_time_range(
    start: Optional[str],
    end: Optional[str],
) -> tuple[datetime, datetime]:
    """解析查询时间范围。

    默认返回最近 1 小时；start/end 为 ISO 8601 字符串。
    Raises:
        ValueError: 时间格式非法或范围超过 30 天。
    """
    now = datetime.now(timezone.utc)

    if end:
        end_dt = ensure_utc(datetime.fromisoformat(end.replace("Z", "+00:00")))
    else:
        end_dt = now

    if start:
        start_dt = ensure_utc(datetime.fromisoformat(start.replace("Z", "+00:00")))
    else:
        start_dt = end_dt - timedelta(hours=1)

    if start_dt > end_dt:
        raise ValueError("start 不能晚于 end")

    if (end_dt - start_dt).days > _MAX_QUERY_RANGE_DAYS:
        raise ValueError(f"单次查询时间范围最大 {_MAX_QUERY_RANGE_DAYS} 天")

    return start_dt, end_dt


def choose_step(start: datetime, end: datetime, requested_step: Optional[str]) -> timedelta:
    """选择查询 step，必要时自动增大以避免返回点数过多。"""
    range_seconds = (end - start).total_seconds()

    if requested_step:
        step_delta = parse_step(requested_step)
    else:
        # 自动选择默认 step
        if range_seconds <= 3600:
            step_delta = timedelta(minutes=1)
        elif range_seconds <= 86400:
            step_delta = timedelta(minutes=5)
        elif range_seconds <= 7 * 86400:
            step_delta = timedelta(hours=1)
        else:
            step_delta = timedelta(days=1)

    # 保证点数不超过 _MAX_POINTS
    step_seconds = step_delta.total_seconds()
    points = range_seconds / step_seconds
    while points > _MAX_POINTS:
        step_seconds *= 2
        points = range_seconds / step_seconds
        step_delta = timedelta(seconds=step_seconds)

    return step_delta


def _aggregate_bucket(values: list[float], aggregator: str, step_seconds: float) -> float:
    """对单个桶内的值执行聚合。"""
    if not values:
        return 0.0

    if aggregator == "avg":
        return sum(values) / len(values)
    if aggregator == "max":
        return max(values)
    if aggregator == "min":
        return min(values)
    if aggregator == "sum":
        return sum(values)
    if aggregator == "count":
        return float(len(values))
    if aggregator == "rate":
        # 计数器：按桶内首尾差值 / 时间跨度 计算每秒变化率
        if len(values) == 1:
            return 0.0
        return (values[-1] - values[0]) / step_seconds

    # 默认 avg
    return sum(values) / len(values)


def aggregate_samples(
    samples: list[MetricSample],
    start: datetime,
    end: datetime,
    step: timedelta,
    aggregator: str,
) -> list[dict]:
    """将原始样本按时间桶聚合。

    Returns:
        [{"timestamp": ISO 字符串, "value": float}, ...]
    """
    if not samples:
        return []

    step_seconds = step.total_seconds()
    buckets: dict[int, list[float]] = {}

    for sample in samples:
        ts = ensure_utc(sample.timestamp)
        # 以 start 为基准，计算桶索引
        bucket_index = int((ts - start).total_seconds() // step_seconds)
        bucket_index = max(0, bucket_index)
        buckets.setdefault(bucket_index, []).append(float(sample.value))

    result = []
    bucket_count = int((end - start).total_seconds() // step_seconds) + 1
    for i in range(bucket_count):
        bucket_time = start + timedelta(seconds=i * step_seconds)
        values = buckets.get(i, [])
        if values:
            value = _aggregate_bucket(values, aggregator, step_seconds)
            result.append({
                "timestamp": bucket_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "value": value,
            })

    return result


def parse_labels(labels_str: Optional[str]) -> dict:
    """解析 labels JSON 字符串。"""
    if not labels_str:
        return {}
    try:
        labels = json.loads(labels_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"labels 不是合法 JSON: {e}")

    if not isinstance(labels, dict):
        raise ValueError("labels 必须是 JSON 对象")
    return labels


def execute_query(
    node_id: str,
    metric: str,
    labels_str: Optional[str],
    start_str: Optional[str],
    end_str: Optional[str],
    step_str: Optional[str],
    aggregator: str,
    backend: Optional[MetricBackend] = None,
) -> dict:
    """执行一次指标查询并返回统一格式。

    Args:
        node_id: 节点 ID。
        metric: 指标名（必填）。
        labels_str: labels JSON 字符串。
        start_str: 起始时间 ISO 字符串。
        end_str: 结束时间 ISO 字符串。
        step_str: step 字符串。
        aggregator: 聚合函数。
        backend: 可选后端实例（测试用）。

    Returns:
        {"metric": str, "step": str, "data": [...]}
    """
    labels = parse_labels(labels_str)
    start, end = parse_time_range(start_str, end_str)
    step = choose_step(start, end, step_str)

    samples = query_metrics(
        node_id=node_id,
        metric_name=metric,
        start=start,
        end=end,
        labels=labels,
        backend=backend,
    )

    data = aggregate_samples(samples, start, end, step, aggregator)
    return {
        "metric": metric,
        "step": format_step(step),
        "data": data,
    }


def get_latest_value(
    node_id: str,
    metric: str,
    labels_str: Optional[str],
    backend: Optional[MetricBackend] = None,
) -> Optional[dict]:
    """查询指定指标的最新值。"""
    labels = parse_labels(labels_str)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)

    samples = query_metrics(
        node_id=node_id,
        metric_name=metric,
        start=start,
        end=end,
        labels=labels,
        backend=backend,
    )

    if not samples:
        return None

    latest = max(samples, key=lambda s: s.timestamp)
    return {
        "metric": metric,
        "timestamp": ensure_utc(latest.timestamp).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "value": float(latest.value),
        "labels": latest.labels,
    }


def get_metric_names(
    node_id: str,
    backend: Optional[MetricBackend] = None,
) -> list[str]:
    """列出某节点最近 1 小时内出现过的指标名。"""
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)

    # 由于后端抽象不支持按 node_id 通配 metric_name 查询，
    # MVP 阶段采用预定义常用指标列表 + 后端查询兜底。
    candidate_names = [
        "cpu_percent",
        "memory_percent",
        "disk_usage_percent",
        "net_bytes_sent",
        "net_bytes_recv",
        "net_packets_err",
        "http_status",
        "http_response_time",
    ]

    found = set()
    for name in candidate_names:
        samples = query_metrics(
            node_id=node_id,
            metric_name=name,
            start=start,
            end=end,
            backend=backend,
        )
        if samples:
            found.add(name)

    return sorted(found)
