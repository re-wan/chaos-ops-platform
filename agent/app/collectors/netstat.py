"""TCP 连接状态采集器。"""

import logging
from datetime import datetime, timezone
from typing import Optional

import psutil

from agent.app.collectors.base import BaseCollector, MetricSample

logger = logging.getLogger("agent.collectors.netstat")

# 关注的核心 TCP 状态：恒定发射（即使为 0），便于告警规则按状态匹配
_TRACKED_STATES = ("established", "time_wait", "close_wait", "listen")


class NetstatCollector(BaseCollector):
    """采集 TCP 连接按状态分类的计数。

    权限不足（AccessDenied，常见于非 root 读取全系统连接）时降级记日志
    并返回空样本，不崩溃、不影响其他采集器。
    """

    name = "netstat"
    default_interval = 15
    metric_names = ("net_connections",)

    def __init__(self, interval: Optional[int] = None):
        super().__init__(interval)

    def collect(self) -> list[MetricSample]:
        """采集 TCP 连接状态计数。"""
        now = datetime.now(timezone.utc)
        samples: list[MetricSample] = []

        try:
            connections = psutil.net_connections(kind="tcp")
        except psutil.AccessDenied:
            logger.warning("读取 TCP 连接权限不足（AccessDenied），跳过本次采集")
            return samples
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"采集 TCP 连接状态失败: {exc}")
            return samples

        counts: dict[str, float] = {state: 0.0 for state in _TRACKED_STATES}
        other = 0.0
        for conn in connections:
            state = (conn.status or "").lower()
            if state in counts:
                counts[state] += 1.0
            else:
                other += 1.0
        counts["other"] = other

        for state, count in counts.items():
            samples.append(
                MetricSample(
                    metric_name="net_connections",
                    value=count,
                    timestamp=now,
                    labels={"state": state},
                )
            )

        return samples
