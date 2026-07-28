"""通用内部事件总线。

与现有 app.core.events（MetricIngestedEvent 专用总线）区分，
本模块提供按 event_type 订阅/发布的轻量总线，供实时推送、审计等场景使用。
"""

from typing import Callable

from app.core.logger import get_logger

logger = get_logger("core.event_bus")

# event_type -> handler list
_handlers: dict[str, list[Callable[[str, dict], None]]] = {}


def subscribe(event_type: str, handler: Callable[[str, dict], None]) -> None:
    """订阅指定类型事件。"""
    _handlers.setdefault(event_type, [])
    if handler not in _handlers[event_type]:
        _handlers[event_type].append(handler)


def unsubscribe(event_type: str, handler: Callable[[str, dict], None]) -> None:
    """取消订阅指定类型事件。"""
    if event_type not in _handlers:
        return
    if handler in _handlers[event_type]:
        _handlers[event_type].remove(handler)


def publish(event_type: str, payload: dict) -> None:
    """发布事件给所有订阅者。

    每个订阅者独立执行，异常被隔离，不影响其他订阅者和发布方。
    """
    for handler in _handlers.get(event_type, []):
        try:
            handler(event_type, payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"事件总线处理失败: event_type={event_type}, error={exc}")
