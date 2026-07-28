"""systemd 服务状态采集器。"""

import logging
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Optional

from agent.app.collectors.base import BaseCollector, MetricSample

logger = logging.getLogger("agent.collectors.service")


class ServiceCollector(BaseCollector):
    """采集 systemd 服务活跃状态（1=active，0=非 active）。

    仅 Linux（存在 systemctl）可用；其他平台或容器环境跳过采集并记 debug 日志。
    单个服务查询失败（超时/异常）记为 0 并继续，不影响其他服务。
    """

    name = "service"
    default_interval = 30
    metric_names = ("service_active",)

    def __init__(
        self,
        interval: Optional[int] = None,
        services: Optional[list[str]] = None,
        timeout_seconds: float = 3.0,
    ):
        super().__init__(interval)
        self.services = services or []
        self.timeout_seconds = timeout_seconds

    def collect(self) -> list[MetricSample]:
        """采集配置的 systemd 服务状态。"""
        if not self.services:
            return []
        if shutil.which("systemctl") is None:
            logger.debug("未找到 systemctl（非 systemd 环境），跳过服务状态采集")
            return []

        now = datetime.now(timezone.utc)
        samples: list[MetricSample] = []

        for service in self.services:
            active = 0.0
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", service],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
                if result.stdout.strip() == "active":
                    active = 1.0
            except subprocess.TimeoutExpired:
                logger.warning(f"查询服务状态超时（{self.timeout_seconds}s）: {service}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"查询服务状态失败: {service}: {exc}")

            samples.append(
                MetricSample(
                    metric_name="service_active",
                    value=active,
                    timestamp=now,
                    labels={"service": service},
                )
            )

        return samples
