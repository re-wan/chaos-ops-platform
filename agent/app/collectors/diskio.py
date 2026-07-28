"""磁盘 IO 采集器。"""

import logging
from datetime import datetime, timezone
from typing import Optional

import psutil

from agent.app.collectors.base import BaseCollector, MetricSample

logger = logging.getLogger("agent.collectors.diskio")


class DiskIOCollector(BaseCollector):
    """采集磁盘 IO 累计读写字节数（per-device）。

    psutil.disk_io_counters(perdisk=True) 返回 None 时（如部分虚拟化环境）
    降级为汇总计数（labels={"device": "all"}）。
    """

    name = "diskio"
    default_interval = 15
    metric_names = ("disk_read_bytes_total", "disk_write_bytes_total")

    def __init__(self, interval: Optional[int] = None):
        super().__init__(interval)

    def collect(self) -> list[MetricSample]:
        """采集磁盘 IO 指标。"""
        now = datetime.now(timezone.utc)
        samples: list[MetricSample] = []

        try:
            per_disk = psutil.disk_io_counters(perdisk=True)
            if per_disk:
                counters = [
                    (device, stats) for device, stats in sorted(per_disk.items())
                ]
            else:
                # 降级：per-disk 不可用时使用汇总计数
                total = psutil.disk_io_counters(perdisk=False)
                if total is None:
                    logger.debug("disk_io_counters 返回 None，跳过磁盘 IO 采集")
                    return samples
                counters = [("all", total)]

            for device, stats in counters:
                labels = {"device": str(device)}
                samples.append(
                    MetricSample(
                        metric_name="disk_read_bytes_total",
                        value=float(stats.read_bytes),
                        timestamp=now,
                        labels=labels.copy(),
                    )
                )
                samples.append(
                    MetricSample(
                        metric_name="disk_write_bytes_total",
                        value=float(stats.write_bytes),
                        timestamp=now,
                        labels=labels.copy(),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"采集磁盘 IO 指标失败: {exc}")

        return samples
