"""Agent 端本地指标聚合（Phase 3 Step 05，对齐 PERFORMANCE_DESIGN §4.1/§6）。

目标：在固定时间窗（默认 10s）内，对同一 (metric_name, labels) 的多个样本
合并为单个统计样本，显著降低上报量与网络/Server 压力。

协议向后兼容：

- 聚合后的样本仍以 ``{metric_name, value, timestamp, labels}`` 形态进入既有
  ``MetricSender``，最终 payload（``{node_id, samples:[...]}``）与未聚合完全一致。
- 每个 (metric, labels) 窗口发射两条样本（批 14 起，``emit_max=True`` 时）：
  - 原指标名，value=窗口 **avg**（算术平均），语义与 v1 完全一致；
  - 派生指标名 ``{metric_name}_max``，value=窗口 **max**，服务于峰值告警场景
    （O2）。派生名沿用 Prometheus 合法字符，老 Server 视为普通新指标直接存储，
    新 Agent 对老 Server 无破坏性；``emit_max=False`` 时退回 v1 仅发均值。
- min/count 在 ``AggregatedSample`` 上计算并暴露，便于测试与后续协议 v2
  （携带统计向量）使用，但不下发，避免继续膨胀指标模型。

因此：旧 Agent（无聚合）继续上报原始点，新 Agent 上报更少的均值+峰值点，Server
接收协议**未变**，两端可混部，不破坏已部署 Agent。

降采样：单 key 在窗口内累计点数超过 ``max_points_per_key`` 时，仍折叠为一个
均值点（本质即降采样），防止异常高频指标打爆内存。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from agent.app.collectors.base import MetricSample

logger = logging.getLogger("agent.aggregator")


@dataclass
class AggregatedSample:
    """一个 (metric, labels) 在时间窗内的聚合结果。"""

    metric_name: str
    labels: dict
    count: int
    total: float
    maximum: float
    minimum: float
    timestamp: datetime  # 窗口内最新样本时间戳

    @property
    def avg(self) -> float:
        return self.total / self.count if self.count else 0.0

    def to_metric_sample(self) -> MetricSample:
        """转为可上报的 MetricSample（value=avg），保持 v1 协议兼容。"""
        return MetricSample(
            metric_name=self.metric_name,
            value=self.avg,
            timestamp=self.timestamp,
            labels=dict(self.labels) if self.labels else {},
        )

    def to_metric_samples(self, emit_max: bool = True) -> list[MetricSample]:
        """转为可上报样本列表：均值样本 + （可选）峰值样本。

        峰值样本以派生指标名 ``{metric_name}_max`` 下发（value=窗口 max），
        与均值样本共用 timestamp/labels；空窗口（count=0）不发射任何样本。
        ``emit_max=False`` 时退回 v1 行为，仅发均值样本。
        """
        if self.count <= 0:
            return []
        samples = [self.to_metric_sample()]
        if emit_max:
            samples.append(
                MetricSample(
                    metric_name=f"{self.metric_name}_max",
                    value=self.maximum,
                    timestamp=self.timestamp,
                    labels=dict(self.labels) if self.labels else {},
                )
            )
        return samples


@dataclass
class _Bucket:
    metric_name: str
    labels: dict
    count: int = 0
    total: float = 0.0
    maximum: float = -math.inf
    minimum: float = math.inf
    latest_ts: datetime = field(default_factory=lambda: datetime.min)

    def add(self, value: float, ts: datetime) -> None:
        self.count += 1
        self.total += value
        if value > self.maximum:
            self.maximum = value
        if value < self.minimum:
            self.minimum = value
        if ts >= self.latest_ts:
            self.latest_ts = ts


class MetricAggregator:
    """固定时间窗的本地聚合器。"""

    def __init__(
        self,
        window_seconds: int = 10,
        max_points_per_key: int = 10000,
        emit_max: bool = True,
    ) -> None:
        self.window_seconds = max(1, int(window_seconds))
        self.max_points_per_key = max(1, int(max_points_per_key))
        # 是否额外发射 {metric_name}_max 峰值样本（批 14，默认开启）。
        self.emit_max = emit_max
        self._buckets: dict[tuple, _Bucket] = {}
        self._window_started_at = time.monotonic()
        self._total_points_in = 0
        self._total_points_out = 0

    @staticmethod
    def _key(sample: MetricSample) -> tuple:
        labels = sample.labels or {}
        # labels 为 dict；用排序后的 items 作为可哈希 key，保证同标签聚合。
        return (sample.metric_name, tuple(sorted(labels.items())))

    def add(self, sample: MetricSample) -> None:
        """加入一个原始样本到当前窗口。"""
        key = self._key(sample)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(
                metric_name=sample.metric_name,
                labels=dict(sample.labels) if sample.labels else {},
                latest_ts=sample.timestamp,
            )
            self._buckets[key] = bucket
        # 超过阈值仍折叠（降采样）：不另存原始点，仅更新统计量，内存恒定。
        bucket.add(float(sample.value), sample.timestamp)
        self._total_points_in += 1
        if bucket.count > self.max_points_per_key:
            # 已折叠统计量，不阻塞；此处仅记录一次告警避免刷屏。
            if bucket.count == self.max_points_per_key + 1:
                logger.warning(
                    f"指标 {sample.metric_name} 在窗口内点数超过 "
                    f"{self.max_points_per_key}，已降采样为均值点"
                )

    def add_many(self, samples: list[MetricSample]) -> None:
        for s in samples:
            self.add(s)

    def flush_due(self, now: Optional[float] = None) -> list[AggregatedSample]:
        """若当前窗口已到期，则发射并清空所有桶，返回聚合结果；否则返回空列表。"""
        now = now if now is not None else time.monotonic()
        if now - self._window_started_at < self.window_seconds:
            return []
        return self.flush_all(now=now)

    def flush_all(self, now: Optional[float] = None) -> list[AggregatedSample]:
        """立即发射并清空所有桶，重置窗口起点。"""
        if not self._buckets:
            self._window_started_at = (
                now if now is not None else time.monotonic()
            )
            return []

        results: list[AggregatedSample] = []
        for bucket in self._buckets.values():
            results.append(
                AggregatedSample(
                    metric_name=bucket.metric_name,
                    labels=bucket.labels,
                    count=bucket.count,
                    total=bucket.total,
                    maximum=bucket.maximum,
                    minimum=bucket.minimum,
                    timestamp=bucket.latest_ts,
                )
            )
        self._total_points_out += len(results)
        self._buckets.clear()
        self._window_started_at = now if now is not None else time.monotonic()
        return results

    @property
    def pending_keys(self) -> int:
        return len(self._buckets)

    @property
    def total_points_in(self) -> int:
        return self._total_points_in

    @property
    def total_points_out(self) -> int:
        return self._total_points_out
