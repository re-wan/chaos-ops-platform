"""网络指标采集器。"""

import logging
from datetime import datetime, timezone
from typing import Optional

import psutil

from agent.app.collectors.base import BaseCollector, MetricSample

logger = logging.getLogger("agent.collectors.network")


class NetworkCollector(BaseCollector):
    """采集网络 IO 统计指标。"""

    name = "network"
    default_interval = 15
    metric_names = ("net_bytes_sent", "net_bytes_recv", "net_packets_err")

    def __init__(
        self,
        interval: Optional[int] = None,
        interface: Optional[str] = None,
    ):
        super().__init__(interval)
        self.interface = interface

    def collect(self) -> list[MetricSample]:
        """采集网络指标。"""
        now = datetime.now(timezone.utc)
        samples: list[MetricSample] = []

        try:
            stats = psutil.net_io_counters(pernic=False)
            if stats is None:
                return samples

            labels = {}
            if self.interface:
                labels["interface"] = self.interface

            samples.append(
                MetricSample(
                    metric_name="net_bytes_sent",
                    value=float(stats.bytes_sent),
                    timestamp=now,
                    labels=labels.copy(),
                )
            )
            samples.append(
                MetricSample(
                    metric_name="net_bytes_recv",
                    value=float(stats.bytes_recv),
                    timestamp=now,
                    labels=labels.copy(),
                )
            )
            samples.append(
                MetricSample(
                    metric_name="net_packets_err",
                    value=float(stats.errin + stats.errout),
                    timestamp=now,
                    labels=labels.copy(),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"采集网络指标失败: {exc}")

        return samples
