"""指标写入 Worker（Phase 3 Step 05）。

消费 ``metric.write`` 队列：把请求线程入队的指标批量落库，并将检测任务派发到
``alert.detect`` 队列，从而把时序库 IO 与告警评估从请求热路径中剥离。

幂等性：同一批样本若因 at-least-once 被重复投递，时序库会写入重复点；
InfluxDB/SQLite 对同 (node, metric, timestamp, labels) 的点具备覆盖/幂等语义，
可接受。检测派发到独立队列，避免与本进程事件总线重复触发。
"""

from datetime import datetime
from typing import Any

from app.core.logger import get_logger
from app.core.queues import QUEUE_ALERT_DETECT
from app.models.metric import MetricSample
from app.services.metrics_ingest import _persist_and_publish

logger = get_logger("workers.metric")


def _parse_timestamp(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    # 异常兜底：用当前 UTC 时间，避免任务进入 DLQ。
    from app.core.utils import now_utc

    return now_utc()


def _dict_to_sample(d: dict) -> MetricSample:
    return MetricSample(
        metric_name=d["metric_name"],
        value=float(d["value"]),
        timestamp=_parse_timestamp(d.get("timestamp")),
        labels=d.get("labels") or {},
    )


def process_metric_write(payload: dict) -> None:
    """处理一个 metric.write 任务：落库 + 派发检测。"""
    node_id = payload.get("node_id")
    raw_samples = payload.get("samples") or []
    if not node_id or not raw_samples:
        logger.warning(f"metric.write 任务缺少必要字段，跳过: {payload!r}")
        return

    samples = [_dict_to_sample(d) for d in raw_samples]

    # 落库但不本地 publish：统一改为入队 alert.detect，由 detector_worker 评估，
    # 避免与同步事件总线重复触发同一规则。
    accepted, dropped = _persist_and_publish(node_id, samples, publish=False)
    if accepted <= 0:
        return

    # 同批内按 (node_id, metric_name) 合并，仅保留每个指标的最新值，
    # 显著降低检测队列压力（检测本就只看最近值）。
    latest: dict[str, MetricSample] = {}
    for s in samples:
        prev = latest.get(s.metric_name)
        if prev is None or s.timestamp >= prev.timestamp:
            latest[s.metric_name] = s

    from app.core.task_queue import get_task_queue

    queue = get_task_queue()
    for s in latest.values():
        queue.enqueue(
            QUEUE_ALERT_DETECT,
            {
                "node_id": node_id,
                "metric_name": s.metric_name,
                "value": float(s.value),
                "timestamp": s.timestamp.isoformat(),
                "labels": dict(s.labels) if s.labels else {},
            },
        )
    logger.debug(
        f"metric.write 处理完成: node_id={node_id}, "
        f"accepted={accepted}, dropped={dropped}, detect_jobs={len(latest)}"
    )
