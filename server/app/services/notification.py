"""通知渠道业务逻辑。

包含渠道 CRUD、配置加解密、脱敏、异步发送、失败重试和事件集成。
"""

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cryptography.fernet import Fernet
from sqlmodel import Session, select

from app.core.backends import get_backend_class
from app.core.config import settings
from app.core.incident_event_bus import record_notification_event
from app.core.logger import get_logger
from app.core.url_safety import validate_outbound_url
from app.models.notification_channel import NotificationChannel
from app.models.notification_log import NotificationLog
from app.services import template_renderer

logger = get_logger("services.notification")

# 敏感字段名，返回前端时脱敏
_SENSITIVE_FIELDS = {"password", "token", "secret", "api_key", "apikey", "access_token"}

# 各渠道类型中承载出站 URL 的 config 键（SSRF 校验对象，收尾修复第 5 批）
_OUTBOUND_URL_KEYS: dict[str, tuple[str, ...]] = {
    "webhook": ("url",),
    "dingtalk": ("webhook",),
    "wecom": ("webhook",),
    "lark": ("webhook",),
    "slack": ("webhook",),
}

# 默认最大重试次数
_DEFAULT_MAX_RETRIES = 3

# 幂等去重窗口：同一事件对同一渠道的 pending 记录在窗口内不重复创建
_DEDUP_WINDOW_SECONDS = 60


def _get_fernet() -> Fernet:
    """从 SECRET_KEY 派生 Fernet 密钥。"""
    key = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_config(config: dict[str, Any]) -> str:
    """加密配置字典，返回 base64 编码的密文字符串。"""
    fernet = _get_fernet()
    plaintext = json.dumps(config, ensure_ascii=False)
    return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_config(encrypted_config: str) -> dict[str, Any]:
    """解密密文字符串，返回配置字典。"""
    fernet = _get_fernet()
    plaintext = fernet.decrypt(encrypted_config.encode("utf-8")).decode("utf-8")
    return json.loads(plaintext)


def mask_config(config: dict[str, Any]) -> dict[str, Any]:
    """将配置中的敏感字段脱敏显示。"""
    masked = {}
    for key, value in config.items():
        if isinstance(key, str) and key.lower() in _SENSITIVE_FIELDS:
            masked[key] = "***"
        elif isinstance(value, dict):
            masked[key] = mask_config(value)
        else:
            masked[key] = value
    return masked


def _validate_channel_outbound_urls(channel_type: str, config: dict[str, Any]) -> None:
    """校验渠道配置中的出站 URL（SSRF 防护）。

    不通过时抛 UrlSafetyError(ValueError)，由 API 层统一转换为 400。
    错误信息保持通用，不泄露内网拓扑细节。
    """
    for key in _OUTBOUND_URL_KEYS.get(channel_type, ()):  # email 等无 URL 键的类型跳过
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            validate_outbound_url(value)


def get_channel(session: Session, channel_id: int) -> Optional[NotificationChannel]:
    """根据 ID 获取通知渠道。"""
    return session.get(NotificationChannel, channel_id)


def list_channels(
    session: Session, channel_type: Optional[str] = None, enabled: Optional[bool] = None
) -> list[NotificationChannel]:
    """列出通知渠道，支持按类型和启用状态过滤。"""
    query = select(NotificationChannel)
    if channel_type is not None:
        query = query.where(NotificationChannel.channel_type == channel_type)
    if enabled is not None:
        query = query.where(NotificationChannel.enabled == enabled)
    query = query.order_by(NotificationChannel.created_at)
    return list(session.exec(query).all())


def create_channel(
    session: Session,
    name: str,
    channel_type: str,
    config: dict[str, Any],
    enabled: bool = True,
    lang: str = "zh",
) -> NotificationChannel:
    """创建通知渠道。"""
    # 校验后端是否存在
    get_backend_class(channel_type)
    # 出站 URL 安全校验（SSRF 防护）
    _validate_channel_outbound_urls(channel_type, config)

    channel = NotificationChannel(
        name=name,
        channel_type=channel_type,
        config=encrypt_config(config),
        enabled=enabled,
        lang=lang,
    )
    session.add(channel)
    session.commit()
    session.refresh(channel)
    logger.info(f"创建通知渠道: id={channel.id}, name={name}, type={channel_type}")
    return channel


def update_channel(
    session: Session,
    channel: NotificationChannel,
    name: Optional[str] = None,
    channel_type: Optional[str] = None,
    config: Optional[dict[str, Any]] = None,
    enabled: Optional[bool] = None,
    lang: Optional[str] = None,
) -> NotificationChannel:
    """更新通知渠道。"""
    if name is not None:
        channel.name = name
    if channel_type is not None:
        get_backend_class(channel_type)
        channel.channel_type = channel_type
    if config is not None:
        # 出站 URL 安全校验（SSRF 防护），按生效后的渠道类型判定 URL 键
        _validate_channel_outbound_urls(channel.channel_type, config)
        channel.config = encrypt_config(config)
    if enabled is not None:
        channel.enabled = enabled
    if lang is not None:
        channel.lang = lang

    channel.updated_at = datetime.now(timezone.utc)
    session.add(channel)
    session.commit()
    session.refresh(channel)
    logger.info(f"更新通知渠道: id={channel.id}, name={channel.name}")
    return channel


def delete_channel(session: Session, channel: NotificationChannel) -> None:
    """删除通知渠道。

    保留历史发送记录，仅删除渠道本身。
    """
    session.delete(channel)
    session.commit()
    logger.info(f"删除通知渠道: id={channel.id}, name={channel.name}")


def channel_to_dict(
    channel: NotificationChannel, mask_sensitive: bool = True
) -> dict[str, Any]:
    """将 NotificationChannel 转换为可序列化的字典。"""
    config = decrypt_config(channel.config)
    if mask_sensitive:
        config = mask_config(config)
    return {
        "id": channel.id,
        "name": channel.name,
        "channel_type": channel.channel_type,
        "enabled": channel.enabled,
        "lang": channel.lang,
        "config": config,
        "created_at": channel.created_at,
        "updated_at": channel.updated_at,
    }


def _find_duplicate_pending_log(
    session: Session,
    channel_id: int,
    event_type: str,
    event_id: Optional[str],
) -> Optional[NotificationLog]:
    """查找同一事件在近期的 pending 记录，用于幂等去重。"""
    window_start = datetime.now(timezone.utc) - timedelta(seconds=_DEDUP_WINDOW_SECONDS)
    statement = select(NotificationLog).where(
        NotificationLog.channel_id == channel_id,
        NotificationLog.event_type == event_type,
        NotificationLog.event_id == event_id,
        NotificationLog.status == "pending",
        NotificationLog.created_at >= window_start,
    )
    return session.exec(statement).first()


def send_notification(
    session: Session,
    event_type: str,
    event_id: Optional[str],
    payload: dict[str, Any],
    channel_ids: Optional[list[int]] = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> list[NotificationLog]:
    """触发通知。

    为每个目标渠道创建 pending 记录，并异步调度发送任务。
    如果未指定 channel_ids，则向所有启用渠道发送。
    """
    if channel_ids is None:
        channels = list_channels(session, enabled=True)
    else:
        channels = []
        for cid in channel_ids:
            channel = get_channel(session, cid)
            if channel is not None and channel.enabled:
                channels.append(channel)

    if not channels:
        logger.info(
            f"没有可用的通知渠道: event_type={event_type}, event_id={event_id}"
        )
        return []

    logs: list[NotificationLog] = []
    for channel in channels:
        # 幂等：同一事件近期已存在 pending 记录则跳过
        duplicate = _find_duplicate_pending_log(
            session, channel.id, event_type, event_id
        )
        if duplicate is not None:
            logger.debug(
                f"通知去重：channel_id={channel.id}, event_type={event_type}, "
                f"event_id={event_id}"
            )
            continue

        log = NotificationLog(
            channel_id=channel.id,
            event_type=event_type,
            event_id=event_id,
            status="pending",
            payload=json.dumps(payload, ensure_ascii=False),
            retry_count=0,
            max_retries=max_retries,
            scheduled_at=datetime.now(timezone.utc),
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        logs.append(log)

        # 异步调度立即执行
        _schedule_process_log(log.id)

    return logs


def _schedule_process_log(log_id: int) -> None:
    """调度一条通知记录的异步处理任务。

    Phase 3 Step 05：当任务队列异步可用时，将投递任务入队 ``notify.send`` 由
    notify_worker 处理（便于多 worker 水平扩展）；否则回退到 APScheduler 立即调度，
    保持 Phase 1/2 的既有行为。
    """
    try:
        from app.core.task_queue import get_task_queue

        queue = get_task_queue()
        if queue.is_async():
            from app.core.queues import QUEUE_NOTIFY_SEND

            queue.enqueue(QUEUE_NOTIFY_SEND, {"log_id": log_id})
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"通知异步入队失败，回退调度器: log_id={log_id}, error={exc}")

    try:
        from app.core.scheduler import get_scheduler

        scheduler = get_scheduler()
        scheduler.add_job(
            process_notification_log,
            args=(log_id,),
            id=f"notification_log_{log_id}_{datetime.now(timezone.utc).isoformat()}",
            replace_existing=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"调度通知任务失败: log_id={log_id}, error={exc}")


def process_notification_log(
    log_id: int, session: Optional[Session] = None
) -> None:
    """处理单条通知记录：发送、重试、写时间线。

    该函数被 APScheduler 异步调用，内部自行创建 Session。
    测试时可通过 session 参数传入测试 Session。
    """
    if session is None:
        from app.core.database import engine

        with Session(engine) as session:
            _process_notification_log_with_session(session, log_id)
    else:
        _process_notification_log_with_session(session, log_id)


def _process_notification_log_with_session(session: Session, log_id: int) -> None:
    """在已有 Session 内处理通知记录。"""
    log = session.get(NotificationLog, log_id)
    if log is None:
        logger.warning(f"通知记录不存在: log_id={log_id}")
        return

    if log.status == "success":
        return

    channel = session.get(NotificationChannel, log.channel_id)
    if channel is None:
        _mark_failed(session, log, "通知渠道不存在")
        return

    if not channel.enabled:
        _mark_failed(session, log, "通知渠道已禁用")
        return

    # 解密配置并构造临时 channel 对象供后端使用
    try:
        decrypted_config = decrypt_config(channel.config)
    except Exception as exc:  # noqa: BLE001
        _mark_failed(session, log, f"配置解密失败: {exc}")
        return

    temp_channel = NotificationChannel(
        id=channel.id,
        name=channel.name,
        channel_type=channel.channel_type,
        config=json.dumps(decrypted_config, ensure_ascii=False),
        enabled=channel.enabled,
    )

    try:
        backend_class = get_backend_class(channel.channel_type)
        backend = backend_class()
        payload = json.loads(log.payload)
        result = backend.send(temp_channel, payload)
    except Exception as exc:  # noqa: BLE001
        result = {"success": False, "error_message": str(exc)}

    if result.get("success"):
        _mark_success(session, log)
    else:
        _schedule_retry_or_fail(session, log, result.get("error_message"))


def _mark_success(session: Session, log: NotificationLog) -> None:
    """标记通知记录为成功。"""
    now = datetime.now(timezone.utc)
    log.status = "success"
    log.sent_at = now
    log.error_message = None
    log.updated_at = now
    session.add(log)
    session.commit()

    _record_timeline(session, log, "notification.sent", "通知发送成功")
    logger.info(f"通知发送成功: log_id={log.id}, channel_id={log.channel_id}")


def _schedule_retry_or_fail(
    session: Session, log: NotificationLog, error_message: Optional[str]
) -> None:
    """失败时更新重试计数，决定是继续重试还是标记失败。"""
    log.retry_count += 1
    log.error_message = error_message
    now = datetime.now(timezone.utc)
    log.updated_at = now

    if log.retry_count < log.max_retries:
        # 指数退避：5s, 10s, 20s
        backoff_seconds = (2 ** log.retry_count) * 5
        log.scheduled_at = now + timedelta(seconds=backoff_seconds)
        log.status = "pending"
        session.add(log)
        session.commit()
        logger.info(
            f"通知失败，进入重试: log_id={log.id}, retry={log.retry_count}, "
            f"next={log.scheduled_at}"
        )
    else:
        _mark_failed(session, log, error_message)


def _mark_failed(
    session: Session, log: NotificationLog, error_message: Optional[str]
) -> None:
    """标记通知记录为失败并记录时间线。"""
    now = datetime.now(timezone.utc)
    log.status = "failed"
    log.error_message = error_message
    log.sent_at = now
    log.updated_at = now
    session.add(log)
    session.commit()

    _record_timeline(session, log, "notification.failed", "通知发送失败")
    logger.warning(
        f"通知发送失败，已达重试上限: log_id={log.id}, "
        f"channel_id={log.channel_id}, error={error_message}"
    )


def _record_timeline(
    session: Session, log: NotificationLog, subtype: str, title: str
) -> None:
    """将通知成功/失败事件写入 Incident 时间线。

    只有 event_type 为 incident 且 event_id 为数字时才写入对应时间线。
    alert 事件通过 Incident 聚合后的创建通知间接记录。
    """
    incident_id = _extract_incident_id(log)
    if incident_id is None:
        return

    try:
        record_notification_event(
            session=session,
            incident_id=incident_id,
            subtype=subtype,
            title=title,
            description=log.error_message
            if subtype == "notification.failed"
            else None,
            metadata={
                "channel_id": log.channel_id,
                "event_type": log.event_type,
                "event_id": log.event_id,
                "retry_count": log.retry_count,
            },
            timestamp=datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"记录通知时间线事件失败: log_id={log.id}, error={exc}")


def _extract_incident_id(log: NotificationLog) -> Optional[int]:
    """从通知记录中提取关联的 Incident ID。"""
    if log.event_type == "incident" and log.event_id is not None:
        try:
            return int(log.event_id)
        except (TypeError, ValueError):
            return None
    return None


def retry_pending_notifications(session: Optional[Session] = None) -> None:
    """轮询并执行到期重试的通知记录。

    由 APScheduler 定时调用。
    """
    if session is None:
        from app.core.database import engine

        with Session(engine) as session:
            _retry_pending_notifications_with_session(session)
    else:
        _retry_pending_notifications_with_session(session)


def _retry_pending_notifications_with_session(session: Session) -> None:
    """在已有 Session 内轮询到期重试记录。"""
    now = datetime.now(timezone.utc)
    statement = select(NotificationLog).where(
        NotificationLog.status == "pending",
        NotificationLog.scheduled_at <= now,
    )
    logs = session.exec(statement).all()
    for log in logs:
        _schedule_process_log(log.id)


def _get_node_name(session: Session, node_id: str) -> str:
    """根据 node_id 获取节点显示名称，找不到时返回 node_id。"""
    from app.models.node import Node

    node = session.exec(select(Node).where(Node.node_id == node_id)).first()
    if node is not None and node.name:
        return node.name
    return node_id


def _build_alert_context(
    session: Session,
    rule_id: int,
    rule_name: str,
    node_id: str,
    severity: str,
    timestamp: datetime,
    metric_context: Optional[dict] = None,
) -> dict[str, Any]:
    """构造告警通知模板上下文。"""
    metric_context = metric_context or {}
    return {
        "alert": {
            "rule_name": rule_name,
            "severity": severity,
            "metric": metric_context.get("metric_name", ""),
            "value": metric_context.get("metric_value", ""),
            "node_id": node_id,
            "node_name": _get_node_name(session, node_id),
        },
        "timestamp": timestamp.isoformat(),
    }


def send_alert_notification(
    session: Session,
    rule_id: int,
    rule_name: str,
    node_id: str,
    severity: str,
    timestamp: datetime,
    channel_ids: Optional[list[int]] = None,
    metric_context: Optional[dict] = None,
) -> list[NotificationLog]:
    """告警 firing 时发送通知。"""
    channels = _get_enabled_target_channels(session, channel_ids)
    logs: list[NotificationLog] = []
    context = _build_alert_context(
        session, rule_id, rule_name, node_id, severity, timestamp, metric_context
    )

    for channel in channels:
        rendered = template_renderer.render_notification(
            session=session,
            event_type="alert.firing",
            channel_type=channel.channel_type,
            context=context,
            lang=channel.lang,
        )
        payload = _build_payload_from_rendered(
            channel.channel_type, rendered, context
        )
        logs.extend(
            send_notification(
                session,
                event_type="alert",
                event_id=str(rule_id),
                payload=payload,
                channel_ids=[channel.id],
            )
        )
    return logs


def _build_incident_context(
    session: Session,
    incident_id: int,
    incident_title: str,
    subtype: str,
    severity: str,
) -> dict[str, Any]:
    """构造事件通知模板上下文。"""
    return {
        "incident": {
            "id": incident_id,
            "title": incident_title,
            "status": subtype.replace("incident.", ""),
            "severity": severity,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def send_incident_notification(
    session: Session,
    incident_id: int,
    incident_title: str,
    subtype: str = "incident.created",
    severity: str = "critical",
    channel_ids: Optional[list[int]] = None,
) -> list[NotificationLog]:
    """Incident 状态变更时发送通知。"""
    channels = _get_enabled_target_channels(session, channel_ids)
    logs: list[NotificationLog] = []
    context = _build_incident_context(
        session, incident_id, incident_title, subtype, severity
    )

    for channel in channels:
        rendered = template_renderer.render_notification(
            session=session,
            event_type=subtype,
            channel_type=channel.channel_type,
            context=context,
            lang=channel.lang,
        )
        payload = _build_payload_from_rendered(
            channel.channel_type, rendered, context
        )
        logs.extend(
            send_notification(
                session,
                event_type="incident",
                event_id=str(incident_id),
                payload=payload,
                channel_ids=[channel.id],
            )
        )
    return logs


def _get_enabled_target_channels(
    session: Session, channel_ids: Optional[list[int]]
) -> list[NotificationChannel]:
    """获取目标启用渠道列表。"""
    if channel_ids is None:
        return list_channels(session, enabled=True)
    channels = []
    for cid in channel_ids:
        channel = get_channel(session, cid)
        if channel is not None and channel.enabled:
            channels.append(channel)
    return channels


def _build_payload_from_rendered(
    channel_type: str, rendered: dict[str, Any], context: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """将渲染结果转换为后端可发送的 payload。"""
    if channel_type == "email":
        return {
            "subject": rendered.get("subject") or "通知",
            "body": rendered["body"],
            "to_addrs": ["admin@example.com"],
        }

    # 从上下文中提取严重级别，供 IM 渠道做颜色区分
    severity = "info"
    if context is not None:
        if "alert" in context:
            severity = context["alert"].get("severity", "info")
        elif "incident" in context:
            severity = context["incident"].get("severity", "info")

    # Webhook / IM 通用 payload：各后端自行组装平台请求体
    return {
        "event_type": "notification",
        "title": rendered.get("subject") or "通知",
        "message": rendered["body"],
        "format": rendered.get("format", "text"),
        "severity": severity,
    }
