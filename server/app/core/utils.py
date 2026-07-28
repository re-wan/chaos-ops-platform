"""全局通用工具函数。

放置不依赖业务模型的纯工具函数，避免循环导入。
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, Request, status

from app.core.config import settings


def now_utc() -> datetime:
    """返回当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite 读出的 datetime 可能为 offset-naive，统一视为 UTC。

    传入 None 时返回 None，便于在模型字段上安全调用。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def resolve_public_server_url(request: Request) -> str:
    """解析对外的 Server URL。

    优先使用配置项 ``PUBLIC_SERVER_URL``；未配置时按以下顺序推断：

    1. ``X-Forwarded-Host`` + ``X-Forwarded-Proto``
    2. ``Host`` + 当前请求 scheme
    3. ``request.base_url``

    如果仍无法确定，抛出 400 错误提示配置 ``PUBLIC_SERVER_URL``。
    """
    if settings.PUBLIC_SERVER_URL:
        return settings.PUBLIC_SERVER_URL.rstrip("/")

    # 反向代理场景：优先使用转发头
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host:
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        return f"{scheme}://{forwarded_host}"

    # 直接访问场景：使用 Host 头
    host = request.headers.get("host")
    if host:
        return f"{request.url.scheme}://{host}"

    # 兜底：FastAPI 解析的 base_url（测试环境通常为 http://testserver）
    base_url = str(request.base_url).rstrip("/")
    if base_url:
        return base_url

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="无法推断 PUBLIC_SERVER_URL，请在配置中设置 PUBLIC_SERVER_URL",
    )
