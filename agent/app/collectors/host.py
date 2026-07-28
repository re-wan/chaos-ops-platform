"""主机指标采集器。"""

import logging
import shutil
from datetime import datetime, timezone
from typing import Optional

import psutil

from agent.app.collectors.base import BaseCollector, MetricSample

logger = logging.getLogger("agent.collectors.host")


class HostCollector(BaseCollector):
    """采集 CPU、内存、磁盘使用率等主机指标。"""

    name = "host"
    default_interval = 15
    metric_names = ("cpu_percent", "memory_percent", "disk_usage_percent")

    def __init__(
        self,
        interval: Optional[int] = None,
        disk_path: str = "/",
        per_cpu: bool = False,
    ):
        super().__init__(interval)
        self.disk_path = disk_path
        self.per_cpu = per_cpu

    def collect(self) -> list[MetricSample]:
        """采集主机指标。"""
        now = datetime.now(timezone.utc)
        samples: list[MetricSample] = []

        # CPU 整体使用率
        try:
            cpu_percent = psutil.cpu_percent(interval=None)
            samples.append(
                MetricSample(
                    metric_name="cpu_percent",
                    value=float(cpu_percent),
                    timestamp=now,
                    labels={"mode": "total"},
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"采集 cpu_percent 失败: {exc}")

        # 每个 CPU 核心使用率（可选）
        if self.per_cpu:
            try:
                per_cpu_values = psutil.cpu_percent(interval=None, percpu=True)
                for idx, value in enumerate(per_cpu_values):
                    samples.append(
                        MetricSample(
                            metric_name="cpu_percent",
                            value=float(value),
                            timestamp=now,
                            labels={"cpu": f"cpu{idx}", "mode": "per_cpu"},
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"采集 per_cpu 失败: {exc}")

        # 内存使用率
        try:
            mem = psutil.virtual_memory()
            samples.append(
                MetricSample(
                    metric_name="memory_percent",
                    value=float(mem.percent),
                    timestamp=now,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"采集 memory_percent 失败: {exc}")

        # 磁盘使用率
        try:
            disk = shutil.disk_usage(self.disk_path)
            disk_usage_percent = (disk.used / disk.total) * 100.0 if disk.total else 0.0
            samples.append(
                MetricSample(
                    metric_name="disk_usage_percent",
                    value=float(disk_usage_percent),
                    timestamp=now,
                    labels={"path": self.disk_path},
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"采集 disk_usage_percent 失败: {exc}")

        return samples
