"""告警去重窗口管理。

去重维度：rule_id + node_id + 排序后的 labels hash。
同一去重键在 firing 后窗口期内再次 firing，只更新时间戳，不发送新通知；
窗口内若状态变为 resolved 再 firing，视为新事件，重新通知。
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.config import settings
from app.models.alert_state import AlertState


def _serialize_labels(labels: Optional[dict]) -> str:
    """将 labels 序列化为稳定字符串用于计算去重键。"""
    data = labels or {}
    return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)


def build_dedup_key(
    rule_id: int,
    node_id: str,
    labels: Optional[dict],
) -> str:
    """构建去重键。

    格式：{rule_id}:{node_id}:{sorted_labels_hash}
    """
    labels_str = _serialize_labels(labels)
    labels_hash = hashlib.sha1(labels_str.encode("utf-8")).hexdigest()[:16]
    return f"{rule_id}:{node_id}:{labels_hash}"


def get_dedup_window_seconds() -> int:
    """返回去重窗口秒数，默认 5 分钟。"""
    return getattr(settings, "ALERT_DEDUP_WINDOW_SECONDS", 300)


def should_notify(
    state: AlertState,
    now: datetime,
    window_seconds: Optional[int] = None,
) -> bool:
    """判断当前 firing 是否应发送通知。

    如果 last_notified_at 为空（resolved 后已被清空或首次 firing），
    或距离上次通知已超过窗口期，则允许通知。
    """
    if window_seconds is None:
        window_seconds = get_dedup_window_seconds()

    last_notified_at = state.last_notified_at
    if last_notified_at is None:
        return True

    if last_notified_at.tzinfo is None:
        last_notified_at = last_notified_at.replace(tzinfo=timezone.utc)

    return now - last_notified_at >= timedelta(seconds=window_seconds)


def record_notification(state: AlertState, now: datetime) -> None:
    """记录本次通知时间。"""
    state.last_notified_at = now
