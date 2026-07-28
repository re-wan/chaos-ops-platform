"""Webhook 通知后端实现。

使用 httpx 发送 HTTP 请求，将 payload 作为 JSON body 发送。
"""

import json
from typing import Any

import httpx

from app.core.logger import get_logger
from app.core.notification_backend import NotificationBackend
from app.core.url_safety import UrlSafetyError, validate_outbound_url
from app.models.notification_channel import NotificationChannel

logger = get_logger("core.backends.webhook")


class WebhookBackend(NotificationBackend):
    """Webhook 通知后端。"""

    def send(
        self, channel: NotificationChannel, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """发送 Webhook 通知。

        config 需包含：url, method, headers, timeout。
        payload 作为 JSON body 发送。
        """
        config = json.loads(channel.config)

        url = config.get("url")
        method = config.get("method", "POST").upper()
        headers = config.get("headers", {})
        timeout = config.get("timeout", 10)

        if not url:
            return {"success": False, "error_message": "缺少 Webhook URL"}

        # 发送前出站安全校验（SSRF 防护）：拦截存量/绕过创建校验的内网 URL
        try:
            validate_outbound_url(url)
        except UrlSafetyError as exc:
            logger.warning(
                f"Webhook 发送被出站安全策略拦截: channel_id={channel.id}, error={exc}"
            )
            return {"success": False, "error_message": str(exc)}

        try:
            response = httpx.request(
                method=method,
                url=url,
                headers=headers,
                json=payload,
                timeout=float(timeout),
            )
            if 200 <= response.status_code < 300:
                logger.info(f"Webhook 发送成功: channel_id={channel.id}, url={url}")
                return {"success": True, "error_message": None}

            error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
            logger.warning(
                f"Webhook 发送失败: channel_id={channel.id}, url={url}, "
                f"status={response.status_code}"
            )
            return {"success": False, "error_message": error_msg}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Webhook 发送失败: channel_id={channel.id}, error={exc}")
            return {"success": False, "error_message": str(exc)}
