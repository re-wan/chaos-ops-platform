"""进程指标采集器。"""

import logging
from datetime import datetime, timezone
from typing import Optional

import psutil

from agent.app.collectors.base import BaseCollector, MetricSample

logger = logging.getLogger("agent.collectors.process")


class ProcessCollector(BaseCollector):
    """采集 CPU 占比最高的 Top N 进程指标。

    按 cpu_percent 降序取前 top_n 个进程，发射每个进程的 CPU/内存占比。
    权限不足（AccessDenied）或已退出（NoSuchProcess）的进程跳过，不影响其他进程。
    """

    name = "process"
    default_interval = 15
    metric_names = ("process_cpu_percent", "process_memory_percent")

    def __init__(
        self,
        interval: Optional[int] = None,
        top_n: int = 10,
    ):
        super().__init__(interval)
        self.top_n = top_n

    def collect(self) -> list[MetricSample]:
        """采集 Top N 进程指标。"""
        now = datetime.now(timezone.utc)
        rows: list[tuple[float, float, int, str]] = []

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                # cpu_percent 相对上次调用采样；首次调用返回 0.0，属正常语义
                cpu = proc.cpu_percent(interval=None)
                mem = proc.memory_percent()
                rows.append((float(cpu), float(mem), proc.info["pid"], proc.info["name"] or ""))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # 进程已退出或权限不足：跳过，不影响其他进程
                continue
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"读取进程信息失败，已跳过: {exc}")
                continue

        # CPU 占比降序取 Top N；cpu 相同按 pid 升序保证输出稳定
        rows.sort(key=lambda r: (-r[0], r[2]))
        top = rows[: self.top_n]

        samples: list[MetricSample] = []
        for cpu, mem, pid, proc_name in top:
            labels = {"pid": str(pid), "name": proc_name}
            samples.append(
                MetricSample(
                    metric_name="process_cpu_percent",
                    value=cpu,
                    timestamp=now,
                    labels=labels.copy(),
                )
            )
            samples.append(
                MetricSample(
                    metric_name="process_memory_percent",
                    value=mem,
                    timestamp=now,
                    labels=labels.copy(),
                )
            )
        return samples
