"""WebSocket 实时推送路由。"""

from fastapi import APIRouter, WebSocket

from app.services.realtime import handle_dashboard_websocket

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/v1/dashboard")
async def dashboard_websocket(websocket: WebSocket) -> None:
    """Dashboard 实时推送 WebSocket 端点。

    认证方式：连接成功后必须第一条消息发送
    ``{"action": "auth", "token": "<session_token>"}``。
    未收到 auth 消息或认证失败直接关闭连接（code 1008）。
    """
    await handle_dashboard_websocket(websocket)
