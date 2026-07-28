"""指标存储后端抽象。"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from app.models.metric import MetricSample


class MetricBackend(ABC):
    """指标存储后端抽象基类。

    所有具体后端（InfluxDB、Mock、未来 TSDB）必须实现以下接口，
    使上层 `metrics_ingest.py` 不依赖具体存储实现。
    """

    @abstractmethod
    def write(self, samples: list[MetricSample]) -> dict:
        """写入样本列表。

        Returns:
            {"accepted": int, "dropped": int}
        """
        raise NotImplementedError

    @abstractmethod
    def query(
        self,
        node_id: str,
        metric_name: str,
        start: datetime,
        end: datetime,
        labels: Optional[dict] = None,
    ) -> list[MetricSample]:
        """按节点、指标名、时间范围查询样本。"""
        raise NotImplementedError

    @abstractmethod
    def delete_before(
        self,
        timestamp: datetime,
        node_id: Optional[str] = None,
        metric_name: Optional[str] = None,
    ) -> int:
        """删除 timestamp 之前的数据，返回删除条数。

        node_id / metric_name 可选：同时提供时仅删除该节点该指标的数据
        （用于优化建议的 per-指标保留期覆盖）；都不提供时为全局删除。
        """
        raise NotImplementedError

    @abstractmethod
    def check_health(self) -> bool:
        """检查后端连接健康状态。"""
        raise NotImplementedError


class MockMetricBackend(MetricBackend):
    """内存 mock 后端，用于测试与本地快速验证。"""

    def __init__(self, healthy: bool = True, raise_on_write: bool = False):
        self.samples: list[MetricSample] = []
        self.healthy = healthy
        self.raise_on_write = raise_on_write
        self.last_write_count = 0

    def write(self, samples: list[MetricSample]) -> dict:
        if self.raise_on_write:
            return {"accepted": 0, "dropped": len(samples)}
        self.samples.extend(samples)
        self.last_write_count = len(samples)
        return {"accepted": len(samples), "dropped": 0}

    def query(
        self,
        node_id: str,
        metric_name: str,
        start: datetime,
        end: datetime,
        labels: Optional[dict] = None,
    ) -> list[MetricSample]:
        labels = labels or {}
        result = []
        for sample in self.samples:
            if sample.labels is None:
                sample_labels = {}
            else:
                sample_labels = sample.labels

            if (
                sample.labels is not None
                and sample.labels.get("node_id") == node_id
                and sample.metric_name == metric_name
                and start <= sample.timestamp <= end
                and all(sample_labels.get(k) == v for k, v in labels.items())
            ):
                result.append(sample)
        return result

    def delete_before(
        self,
        timestamp: datetime,
        node_id: Optional[str] = None,
        metric_name: Optional[str] = None,
    ) -> int:
        def _keep(s: MetricSample) -> bool:
            if s.timestamp >= timestamp:
                return True
            if node_id is not None and (s.labels or {}).get("node_id") != node_id:
                return True
            if metric_name is not None and s.metric_name != metric_name:
                return True
            return False

        kept = [s for s in self.samples if _keep(s)]
        removed = len(self.samples) - len(kept)
        self.samples = kept
        return removed

    def check_health(self) -> bool:
        return self.healthy
