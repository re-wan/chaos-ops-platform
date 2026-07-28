"""告警检测 Worker（Phase 3 Step 05）。

消费 ``alert.detect`` 队列：对单个 (node, metric) 事件驱动告警检测引擎评估相关规则。
检测器内部已按 metric_name 建立规则倒排索引，单条事件只评估命中的少量规则，
因此一个事件一个任务在规模化下仍保持低延迟。

幂等性：检测基于 metric_cache 最近值 + AlertState 状态机，重复事件不会产生
重复 firing（状态机与去重层保证），符合 at-least-once 语义。
"""

from datetime import datetime
from typing import Any

from app.core.events import MetricIngestedEvent
from app.core.logger import get_logger

logger = get_logger("workers.detector")


def _parse_timestamp(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    from app.core.utils import now_utc

    return now_utc()


def process_detect(payload: dict) -> None:
    """处理一个 alert.detect 任务：驱动告警检测引擎评估。"""
    # 延迟导入，避免与检测器初始化形成循环依赖。
    from app.services.alert_detector import get_alert_detector

    detector = get_alert_detector()
    if detector is None:
        # 检测器未启用或尚未初始化（如非 leader worker），安全跳过。
        logger.debug("告警检测器不可用，跳过 detect 任务")
        return

    event = MetricIngestedEvent(
        node_id=payload["node_id"],
        metric_name=payload["metric_name"],
        value=float(payload["value"]),
        timestamp=_parse_timestamp(payload.get("timestamp")),
        labels=payload.get("labels") or {},
    )
    detector.process_event(event)
