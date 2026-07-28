"""告警检测器后台任务。

负责定时刷新规则索引和清理过期缓存。
"""

from app.core.scheduler import safe_task
from app.services.alert_detector import get_alert_detector


@safe_task
def reload_detector_rules_task() -> None:
    """定时重新加载告警规则索引。"""
    detector = get_alert_detector()
    if detector is not None:
        detector.reload_rules()


@safe_task
def cleanup_metric_cache_task() -> None:
    """定时清理过期指标缓存。"""
    detector = get_alert_detector()
    if detector is not None:
        detector.cleanup_cache()
