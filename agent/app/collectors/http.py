"""HTTP 端点检查采集器。"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from agent.app.collectors.base import BaseCollector, MetricSample

logger = logging.getLogger("agent.collectors.http")


class HTTPCollector(BaseCollector):
    """对目标 URL 执行 HTTP GET，采集状态码与响应时间。"""

    name = "http"
    default_interval = 30
    metric_names = ("http_status", "http_response_time")

    def __init__(
        self,
        targets: Optional[list[str]] = None,
        interval: Optional[int] = None,
        timeout: float = 10.0,
    ):
        super().__init__(interval)
        self.targets = targets or []
        self.timeout = timeout

    def collect(self) -> list[MetricSample]:
        """采集 HTTP 检查指标。"""
        now = datetime.now(timezone.utc)
        samples: list[MetricSample] = []

        for url in self.targets:
            try:
                start = time.monotonic()
                response = httpx.get(url, timeout=self.timeout, follow_redirects=True)
                elapsed_ms = (time.monotonic() - start) * 1000.0

                samples.append(
                    MetricSample(
                        metric_name="http_status",
                        value=float(response.status_code),
                        timestamp=now,
                        labels={"url": url},
                    )
                )
                samples.append(
                    MetricSample(
                        metric_name="http_response_time",
                        value=float(elapsed_ms),
                        timestamp=now,
                        labels={"url": url},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"HTTP 检查失败 ({url}): {exc}")
                # 目标不可达时记录状态码 0，便于后续告警判断
                samples.append(
                    MetricSample(
                        metric_name="http_status",
                        value=0.0,
                        timestamp=now,
                        labels={"url": url},
                    )
                )

        return samples
