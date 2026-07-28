"""事件时间线统一写入接口。

所有外部模块（告警检测、自愈执行、用户操作、通知渠道）产生的时间线事件
必须通过此模块写入，确保格式一致、异常隔离、元数据大小可控。
"""

import json
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session

from app.core.logger import get_logger
from app.models.incident_event import IncidentEvent

logger = get_logger("core.incident_event_bus")

# 元数据大小限制：16KB
_MAX_METADATA_BYTES = 16 * 1024


def _serialize_metadata(metadata: Optional[dict]) -> str:
    """将元数据序列化为 JSON 字符串，超过大小限制时截断并记录 warning。

    截断策略：当序列化后字节数超过 16KB 时，替换为一个标记对象，保留原始大小
    信息，避免存储非法 JSON 或过度膨胀。
    """
    if metadata is None:
        return "{}"

    try:
        json_str = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        logger.warning(f"时间线元数据序列化失败: {exc}")
        return json.dumps({"_serialization_error": str(exc)}, ensure_ascii=False)

    json_bytes = json_str.encode("utf-8")
    if len(json_bytes) <= _MAX_METADATA_BYTES:
        return json_str

    logger.warning(
        f"时间线元数据超过 {_MAX_METADATA_BYTES} 字节，已截断。"
        f"原始大小: {len(json_bytes)} 字节"
    )
    truncated = {
        "_truncated": True,
        "_original_size_bytes": len(json_bytes),
        "_max_size_bytes": _MAX_METADATA_BYTES,
        "_reason": "metadata exceeds size limit",
    }
    return json.dumps(truncated, ensure_ascii=False, sort_keys=True)


def record_incident_event(
    session: Session,
    incident_id: int,
    event_type: str,
    event_subtype: str,
    title: str,
    description: Optional[str] = None,
    metadata: Optional[dict] = None,
    source: str = "system",
    created_by: Optional[int] = None,
    timestamp: Optional[datetime] = None,
) -> Optional[IncidentEvent]:
    """统一写入时间线事件。

    异常被捕获隔离，不会抛给调用方，避免影响主流程。

    Returns:
        写入成功返回 IncidentEvent，失败返回 None。
    """
    try:
        event = IncidentEvent(
            incident_id=incident_id,
            event_type=event_type,
            event_subtype=event_subtype,
            title=title,
            description=description,
            event_metadata=_serialize_metadata(metadata),
            source=source,
            created_by=created_by,
            timestamp=timestamp or datetime.now(timezone.utc),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        logger.debug(
            f"记录时间线事件: incident_id={incident_id}, "
            f"subtype={event_subtype}, source={source}"
        )
        return event
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            f"记录时间线事件失败: incident_id={incident_id}, "
            f"subtype={event_subtype}, error={exc}"
        )
        return None


def record_notification_event(
    session: Session,
    incident_id: int,
    subtype: str,
    title: str,
    description: Optional[str] = None,
    metadata: Optional[dict] = None,
    created_by: Optional[int] = None,
    timestamp: Optional[datetime] = None,
) -> Optional[IncidentEvent]:
    """记录通知渠道时间线事件（预留接口，Step 17/18 实现通知后调用）。

    subtype 应为 "notification.sent" 或 "notification.failed"。
    """
    return record_incident_event(
        session=session,
        incident_id=incident_id,
        event_type="notification",
        event_subtype=subtype,
        title=title,
        description=description,
        metadata=metadata,
        source="notifier",
        created_by=created_by,
        timestamp=timestamp,
    )
