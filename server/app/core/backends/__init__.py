"""通知后端注册。

集中管理所有通知后端的映射关系，便于扩展和查找。
"""

from app.core.backends.email import EmailBackend
from app.core.backends.webhook import WebhookBackend
from app.core.notification_backend import NotificationBackend

_BACKENDS: dict[str, type[NotificationBackend]] = {
    "email": EmailBackend,
    "webhook": WebhookBackend,
}


def get_backend_class(channel_type: str) -> type[NotificationBackend]:
    """根据渠道类型获取对应后端类。"""
    backend_class = _BACKENDS.get(channel_type)
    if backend_class is None:
        raise ValueError(f"不支持的通知渠道类型: {channel_type}")
    return backend_class


def register_backend(
    channel_type: str, backend_class: type[NotificationBackend]
) -> None:
    """注册新的通知后端（供 Phase 2 IM 渠道扩展使用）。"""
    _BACKENDS[channel_type] = backend_class


# IM 后端（付费功能）：三版物理分包会删除对应文件，
# 逐个防御式 import 并在文件存在时注册（全量/dev/企业版行为不变）。
try:
    from app.core.backends.dingtalk import DingTalkBackend

    register_backend("dingtalk", DingTalkBackend)
except ImportError:
    pass

try:
    from app.core.backends.wecom import WeComBackend

    register_backend("wecom", WeComBackend)
except ImportError:
    pass

try:
    from app.core.backends.lark import LarkBackend

    register_backend("lark", LarkBackend)
except ImportError:
    pass

try:
    from app.core.backends.slack import SlackBackend

    register_backend("slack", SlackBackend)
except ImportError:
    pass
