"""应用内部事件定义与极简事件总线。

当前仅支持 MetricIngestedEvent，用于指标写入后驱动告警检测引擎。
事件处理异常被捕获并记录，不影响事件发布方。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.core.logger import get_logger

logger = get_logger("core.events")


@dataclass
class MetricIngestedEvent:
    """指标已成功写入时序数据库事件。"""

    node_id: str
    metric_name: str
    value: float
    timestamp: datetime
    labels: dict


# 事件订阅者列表
_metric_ingested_handlers: list[Callable[[MetricIngestedEvent], None]] = []


def subscribe_metric_ingested(
    handler: Callable[[MetricIngestedEvent], None],
) -> None:
    """订阅 MetricIngestedEvent。"""
    _metric_ingested_handlers.append(handler)


def unsubscribe_metric_ingested(
    handler: Callable[[MetricIngestedEvent], None],
) -> None:
    """取消订阅 MetricIngestedEvent。"""
    if handler in _metric_ingested_handlers:
        _metric_ingested_handlers.remove(handler)


def reset_metric_ingested_handlers() -> None:
    """清空所有 MetricIngestedEvent 订阅者（仅用于测试隔离）。"""
    _metric_ingested_handlers.clear()


def publish_metric_ingested(event: MetricIngestedEvent) -> None:
    """发布 MetricIngestedEvent 给所有订阅者。

    每个订阅者独立执行，异常被隔离，不影响其他订阅者和发布方。
    """
    for handler in _metric_ingested_handlers:
        try:
            handler(event)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"MetricIngestedEvent 处理失败: {exc}")
