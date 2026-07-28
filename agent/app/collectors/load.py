"""系统负载采集器。"""

import logging
from datetime import datetime, timezone
from typing import Optional

import psutil

from agent.app.collectors.base import BaseCollector, MetricSample

logger = logging.getLogger("agent.collectors.load")


class LoadCollector(BaseCollector):
    """采集系统负载（load1/load5/load15）。

    仅 Unix 可用：psutil.getloadavg 在 Windows 上不可用（AttributeError），
    本采集器在 Windows 上跳过采集并记 debug 日志，不影响其他采集器。
    """

    name = "load"
    default_interval = 15
    metric_names = ("load1", "load5", "load15")

    def __init__(self, interval: Optional[int] = None):
        super().__init__(interval)

    def collect(self) -> list[MetricSample]:
        """采集系统负载指标。"""
        if not hasattr(psutil, "getloadavg"):
            logger.debug("当前平台不支持 getloadavg（Windows），跳过负载采集")
            return []

        now = datetime.now(timezone.utc)
        samples: list[MetricSample] = []

        try:
            load1, load5, load15 = psutil.getloadavg()
            for name, value in (("load1", load1), ("load5", load5), ("load15", load15)):
                samples.append(
                    MetricSample(
                        metric_name=name,
                        value=float(value),
                        timestamp=now,
                    )
                )
        except (OSError, AttributeError) as exc:
            logger.debug(f"采集系统负载失败，已跳过: {exc}")

        return samples
