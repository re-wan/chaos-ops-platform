"""WebSocket 实时推送服务。

管理 Dashboard WebSocket 连接、订阅、事件分发与断线补偿。
"""

import asyncio
import json
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect
from sqlmodel import Session, select, update

from app.core.utils import ensure_utc, now_utc
from app.core import database as database_module
from app.core.event_bus import subscribe, unsubscribe
from app.core.logger import get_logger
from app.models.user import User
from app.models.user_session import UserSession

logger = get_logger("services.realtime")

# 最近事件环形缓冲区：保留最多 2000 条或 60 分钟内的事件，用于断线补偿
_MAX_RECENT_EVENTS = 2000
_MAX_RECENT_AGE_SECONDS = 3600

# 用户可见事件映射：topic -> [event_type]
_TOPIC_EVENT_TYPES = {
    "alerts": ["alert.firing", "alert.resolved"],
    "incidents": [
        "incident.created",
        "incident.acknowledged",
        "incident.resolved",
        "incident.closed",
        "incident.merged",
    ],
    "nodes": ["node.online", "node.offline"],
    "heal": ["heal.executed"],
    "notifications": ["notification.sent"],
}

# viewer 角色只允许订阅的 topic
_VIEWER_ALLOWED_TOPICS = {"alerts", "incidents", "nodes"}

# 管理类 topic，viewer 订阅时会被静默忽略
_ADMIN_TOPICS = {"heal", "notifications"}


class Connection:
    """单个 WebSocket 连接的状态。"""

    def __init__(self, websocket: WebSocket, user: User):
        self.websocket = websocket
        self.user = user
        self.subscribed_topics: set[str] = set()
        self.lock = asyncio.Lock()

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"

    def allowed_topics(self, topics: list[str]) -> set[str]:
        """根据用户角色过滤可用 topic。"""
        if self.is_admin:
            return set(topics) & set(_TOPIC_EVENT_TYPES.keys())
        return (set(topics) & _VIEWER_ALLOWED_TOPICS)

    def is_event_allowed(self, event_type: str) -> bool:
        """判断某事件类型是否对当前连接可见。"""
        for topic in self.subscribed_topics:
            if event_type in _TOPIC_EVENT_TYPES.get(topic, []):
                if self.is_admin or topic in _VIEWER_ALLOWED_TOPICS:
                    return True
        return False


class RealtimeService:
    """实时推送服务：连接管理、事件缓冲、订阅过滤。"""

    def __init__(self) -> None:
        self._connections: set[Connection] = set()
        self._connections_lock = asyncio.Lock()
        self._recent_events: deque[dict] = deque(maxlen=_MAX_RECENT_EVENTS)
        self._event_types_of_interest: set[str] = set()
        for types in _TOPIC_EVENT_TYPES.values():
            self._event_types_of_interest.update(types)
        self._event_handler = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self) -> None:
        """订阅内部事件总线。"""
        self._event_handler = self._on_event
        for event_type in self._event_types_of_interest:
            subscribe(event_type, self._event_handler)
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        logger.info("实时推送服务已启动")

    def stop(self) -> None:
        """取消订阅内部事件总线。"""
        if self._event_handler is not None:
            for event_type in self._event_types_of_interest:
                unsubscribe(event_type, self._event_handler)
        logger.info("实时推送服务已关闭")

    def _on_event(self, event_type: str, payload: dict) -> None:
        """内部事件总线回调：缓存并广播事件。

        该回调可能从同步业务代码中调用，因此需要线程安全地调度广播任务。
        """
        event = self._build_event(event_type, payload)
        self._buffer_event(event)
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(
                self._schedule_broadcast, event
            )

    def _schedule_broadcast(self, event: dict) -> None:
        """在事件循环中调度广播任务。"""
        try:
            asyncio.create_task(self._broadcast(event))
        except RuntimeError:
            pass

    def _build_event(self, event_type: str, payload: dict) -> dict:
        """构造对外推送消息。"""
        return {
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "payload": payload,
        }

    def _buffer_event(self, event: dict) -> None:
        """将事件加入环形缓冲区。"""
        self._recent_events.append(event)
        self._cleanup_old_events()

    def _cleanup_old_events(self) -> None:
        """清理超过最大保留时长的事件。"""
        cutoff = datetime.now(timezone.utc).timestamp() - _MAX_RECENT_AGE_SECONDS
        while self._recent_events:
            first = self._recent_events[0]
            try:
                ts = datetime.fromisoformat(first["timestamp"].replace("Z", "+00:00"))
                if ts.timestamp() < cutoff:
                    self._recent_events.popleft()
                else:
                    break
            except Exception:  # noqa: BLE001
                break

    async def _broadcast(self, event: dict) -> None:
        """广播事件给所有已订阅且可见的连接。"""
        event_type = event.get("event_type", "")
        disconnected: list[Connection] = []
        async with self._connections_lock:
            connections = list(self._connections)

        for conn in connections:
            if not conn.is_event_allowed(event_type):
                continue
            try:
                async with conn.lock:
                    await conn.websocket.send_json(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"WebSocket 推送失败，准备断开: user={conn.user.id}, error={exc}")
                disconnected.append(conn)

        for conn in disconnected:
            await self.remove_connection(conn)

    async def add_connection(self, conn: Connection) -> None:
        async with self._connections_lock:
            self._connections.add(conn)

    async def remove_connection(self, conn: Connection) -> None:
        async with self._connections_lock:
            self._connections.discard(conn)
        try:
            await conn.websocket.close()
        except Exception:  # noqa: BLE001
            pass

    async def handle_client_message(self, conn: Connection, raw_message: str) -> None:
        """处理客户端发送的 WebSocket 消息。"""
        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError:
            await self._send_error(conn, "无效的消息格式")
            return

        action = message.get("action")
        if action == "subscribe":
            await self._handle_subscribe(conn, message)
        elif action == "unsubscribe":
            await self._handle_unsubscribe(conn, message)
        elif action == "ping":
            await self._handle_ping(conn)
        elif action == "sync_since":
            await self._handle_sync_since(conn, message)
        else:
            await self._send_error(conn, f"未知 action: {action}")

    async def _handle_subscribe(self, conn: Connection, message: dict) -> None:
        topics = message.get("topics", [])
        if not isinstance(topics, list):
            await self._send_error(conn, "topics 必须是列表")
            return
        allowed = conn.allowed_topics(topics)
        # viewer 订阅管理类 topic 时静默忽略，不报错
        conn.subscribed_topics.update(allowed)
        await conn.websocket.send_json({
            "action": "subscribed",
            "topics": sorted(list(conn.subscribed_topics)),
        })

    async def _handle_unsubscribe(self, conn: Connection, message: dict) -> None:
        topics = message.get("topics", [])
        if not isinstance(topics, list):
            await self._send_error(conn, "topics 必须是列表")
            return
        conn.subscribed_topics.difference_update(topics)
        await conn.websocket.send_json({
            "action": "unsubscribed",
            "topics": sorted(list(conn.subscribed_topics)),
        })

    async def _handle_ping(self, conn: Connection) -> None:
        await conn.websocket.send_json({
            "action": "pong",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })

    async def _handle_sync_since(self, conn: Connection, message: dict) -> None:
        since_str = message.get("since")
        if not since_str:
            await self._send_error(conn, "sync_since 必须提供 since 字段")
            return
        try:
            since = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        except ValueError:
            await self._send_error(conn, "since 时间格式无效")
            return

        missed = self._get_missed_events(conn, since)
        for event in missed:
            try:
                async with conn.lock:
                    await conn.websocket.send_json(event)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"同步事件发送失败: user={conn.user.id}, error={exc}")
                await self.remove_connection(conn)
                return

        await conn.websocket.send_json({
            "action": "sync_complete",
            "since": since_str,
            "count": len(missed),
        })

    def _get_missed_events(self, conn: Connection, since: datetime) -> list[dict]:
        """按时间顺序返回连接可见的 missed events。"""
        missed: list[dict] = []
        for event in self._recent_events:
            try:
                ts = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts > since and conn.is_event_allowed(event.get("event_type", "")):
                missed.append(event)
        return missed

    async def _send_error(self, conn: Connection, detail: str) -> None:
        try:
            async with conn.lock:
                await conn.websocket.send_json({"action": "error", "detail": detail})
        except Exception:  # noqa: BLE001
            pass

    def get_stats(self) -> dict:
        """返回连接统计。"""
        return {
            "connections": len(self._connections),
            "buffered_events": len(self._recent_events),
        }


# 全局单例
_service: Optional[RealtimeService] = None
_service_lock = asyncio.Lock()


async def get_or_create_service() -> RealtimeService:
    """获取或创建全局实时推送服务。"""
    global _service
    if _service is None:
        async with _service_lock:
            if _service is None:
                _service = RealtimeService()
                _service.start()
    return _service


def get_realtime_service() -> Optional[RealtimeService]:
    """获取当前全局实时推送服务（可能为 None）。"""
    return _service


async def stop_realtime_service() -> None:
    """关闭全局实时推送服务。"""
    global _service
    if _service is not None:
        _service.stop()
        # 关闭所有连接
        async with _service._connections_lock:
            connections = list(_service._connections)
        for conn in connections:
            await _service.remove_connection(conn)
        _service = None


def _get_session_slide_delta():
    from datetime import timedelta

    from app.core.config import settings

    return timedelta(days=settings.USER_SESSION_SLIDE_DAYS)


def _user_copy(user: User) -> User:
    """返回一个与 Session 解耦的 User 副本，避免 WebSocket 长连接中访问已关闭 Session。"""
    return User(
        id=user.id,
        username=user.username,
        hashed_password=user.hashed_password,
        is_active=user.is_active,
        created_at=user.created_at,
        role=user.role,
        tenant_id=user.tenant_id,
    )


def _authenticate_token(session: Session, token: str) -> Optional[User]:
    """通过 session token 校验用户。"""
    if not token or not token.startswith("u_"):
        return None
    user_session = session.exec(
        select(UserSession).where(
            UserSession.token == token,
            UserSession.is_active == True,  # noqa: E712
        )
    ).first()
    if user_session is None:
        return None

    now = now_utc()
    if now > ensure_utc(user_session.expires_at):
        return None
    slide_limit = ensure_utc(user_session.last_used_at) + _get_session_slide_delta()
    if now > slide_limit:
        return None

    user = session.get(User, user_session.user_id)
    if user is None or not user.is_active:
        return None

    # 滑动窗口更新 last_used_at：60 秒内只写一次，降低并发写竞争
    last_used = ensure_utc(user_session.last_used_at)
    if last_used is None or (now - last_used) > timedelta(seconds=60):
        session.exec(
            update(UserSession)
            .where(UserSession.id == user_session.id)
            .values(last_used_at=now)
        )
        session.commit()

    return _user_copy(user)


async def authenticate_websocket(websocket: WebSocket, message_token: Optional[str] = None) -> Optional[User]:
    """WebSocket 认证：优先使用 httpOnly cookie，其次使用消息中的 token。

    兼容 Web 控制台（cookie）和脚本/测试（显式 token）。
    """
    with Session(database_module.engine) as session:
        cookie_token = websocket.cookies.get("access_token")
        if cookie_token:
            user = _authenticate_token(session, cookie_token)
            if user is not None:
                return user

        if message_token:
            return _authenticate_token(session, message_token)

    return None


async def handle_dashboard_websocket(websocket: WebSocket) -> None:
    """处理 /ws/v1/dashboard WebSocket 连接。

    认证流程：
        1. 接受连接后等待第一条客户端消息（5 秒超时）。
        2. 第一条消息必须是 action="auth"；其中可携带 token（脚本/测试兼容）。
        3. Web 控制台通过 httpOnly cookie 鉴权，因此 token 可为空。
        4. 未收到 auth 消息、消息格式无效、认证失败或超时，
           直接关闭连接（code 1008）。
    """
    await websocket.accept()

    try:
        first_message = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
    except asyncio.TimeoutError:
        await websocket.close(code=1008, reason="认证超时")
        return
    except WebSocketDisconnect:
        # 客户端在认证前断开，无需记录错误
        return

    try:
        message = json.loads(first_message)
    except json.JSONDecodeError:
        await websocket.close(code=1008, reason="无效的消息格式")
        return

    if message.get("action") != "auth":
        await websocket.close(code=1008, reason="缺少认证消息")
        return

    token = message.get("token") or None
    user = await authenticate_websocket(websocket, message_token=token)
    if user is None:
        await websocket.close(code=1008, reason="未认证或 Token 无效")
        return

    logger.info(f"WebSocket 认证成功: user={user.id}")

    service = await get_or_create_service()
    conn = Connection(websocket, user)
    await service.add_connection(conn)

    try:
        while True:
            raw_message = await websocket.receive_text()
            await service.handle_client_message(conn, raw_message)
    except WebSocketDisconnect:
        logger.info(f"WebSocket 断开: user={user.id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"WebSocket 异常: user={user.id}, error={exc}")
    finally:
        await service.remove_connection(conn)
