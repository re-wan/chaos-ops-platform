"""采集器基类与通用数据模型。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class MetricSample:
    """单个指标样本。"""

    metric_name: str
    value: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    labels: Optional[dict] = None

    def to_dict(self) -> dict:
        """序列化为字典。"""
        return {
            "metric_name": self.metric_name,
            "value": self.value,
            "timestamp": self.timestamp.isoformat().replace("+00:00", "Z"),
            "labels": self.labels or {},
        }


class BaseCollector(ABC):
    """指标采集器基类。"""

    name: str = "base"
    default_interval: int = 15
    # 该采集器发射的指标名集合：用于 Server 下发的 collector_intervals
    # （按 metric_name 键）反向定位到采集器，运行中调整采集间隔。
    metric_names: tuple[str, ...] = ()

    def __init__(self, interval: Optional[int] = None):
        """初始化采集器。

        Args:
            interval: 采集间隔（秒），默认使用 default_interval。
        """
        self.interval = interval if interval is not None else self.default_interval

    @abstractmethod
    def collect(self) -> list[MetricSample]:
        """执行一次采集，返回样本列表。

        子类抛出异常时由调用方捕获，避免影响其他采集器。
        """
        raise NotImplementedError
