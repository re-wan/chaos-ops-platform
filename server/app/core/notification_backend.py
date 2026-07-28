"""通知后端抽象基类。

所有具体通知后端（Email、Webhook、IM）必须继承此类并实现 send 方法。
"""

from abc import ABC, abstractmethod
from typing import Any

from app.models.notification_channel import NotificationChannel


class NotificationBackend(ABC):
    """通知后端抽象。"""

    @abstractmethod
    def send(
        self, channel: NotificationChannel, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """发送通知。

        Args:
            channel: 通知渠道 ORM 对象，config 字段已由调用方解密。
            payload: 发送内容，由通知服务根据事件类型构造。

        Returns:
            {"success": bool, "error_message": Optional[str]}
        """
