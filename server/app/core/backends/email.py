"""Email 通知后端实现。

使用 Python 标准库 smtplib + email.mime.text 发送邮件。
"""

import json
import smtplib
from email.mime.text import MIMEText
from typing import Any

from app.core.logger import get_logger
from app.core.notification_backend import NotificationBackend
from app.models.notification_channel import NotificationChannel

logger = get_logger("core.backends.email")


class EmailBackend(NotificationBackend):
    """Email 通知后端。"""

    def send(
        self, channel: NotificationChannel, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """发送邮件通知。

        config 需包含：smtp_host, smtp_port, username, password, use_tls, from_addr。
        payload 需包含：to_addrs, subject, body。
        """
        config = json.loads(channel.config)

        smtp_host = config.get("smtp_host")
        smtp_port = config.get("smtp_port", 25)
        username = config.get("username")
        password = config.get("password")
        use_tls = config.get("use_tls", False)
        from_addr = config.get("from_addr", username)

        to_addrs = payload.get("to_addrs")
        subject = payload.get("subject", "")
        body = payload.get("body", "")

        if not smtp_host:
            return {"success": False, "error_message": "缺少 SMTP 服务器地址"}
        if not username:
            return {"success": False, "error_message": "缺少 SMTP 用户名"}
        if not to_addrs:
            return {"success": False, "error_message": "缺少收件人地址"}

        # 统一处理单/多收件人
        if isinstance(to_addrs, str):
            to_addrs = [to_addrs]

        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = from_addr or username
            msg["To"] = ", ".join(to_addrs)

            with smtplib.SMTP(smtp_host, int(smtp_port), timeout=30) as server:
                if use_tls:
                    server.starttls()
                if password:
                    server.login(username, password)
                server.sendmail(from_addr or username, to_addrs, msg.as_string())

            logger.info(f"邮件发送成功: channel_id={channel.id}, to={to_addrs}")
            return {"success": True, "error_message": None}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"邮件发送失败: channel_id={channel.id}, error={exc}")
            return {"success": False, "error_message": str(exc)}
