"""FastAPI 依赖：数据库 Session、当前用户认证、当前节点认证。"""

import threading
import time
from collections import OrderedDict
from datetime import timedelta
from typing import Generator, Optional

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, select, update

from app.core.config import settings
from app.core.error_messages import error_detail
from app.core.licensing import check_feature_enabled
from app.core.utils import ensure_utc, now_utc
from app.schemas.page import DEFAULT_PAGE, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, PageParams
from app.core.database import get_session
from app.models.node import Node
from app.models.user import User
from app.models.user_session import UserSession

# auto_error=False：让业务代码决定如何响应缺失/无效 Token
security = HTTPBearer(auto_error=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：为每个请求生成一个数据库 Session。"""
    yield from get_session()


def _extract_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    """优先从 httpOnly cookie 读取 access_token，其次 fallback 到 Authorization Bearer。"""
    cookie_token = request.cookies.get("access_token")
    if cookie_token:
        return cookie_token
    if credentials is not None and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    return None


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """从 cookie 或 Authorization: Bearer <token> 中提取并校验当前用户。

    校验逻辑：
        1. 优先读取 httpOnly cookie 中的 access_token；缺失时 fallback 到 Bearer Token。
        2. Token 必须存在于 user_sessions 表中且 is_active=True。
        3. Token 未超过 expires_at（绝对过期时间）。
        4. Token 未超过 last_used_at + 滑动有效期（默认 7 天）。
        5. 对应用户存在且 is_active=True。
        6. 校验通过后按 60 秒滑动窗口更新 last_used_at。

    注意：任何错误信息中不得包含原始 Token，也不得将 Token 写入日志。
    """
    token = _extract_token(request, credentials)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail(request, "auth.invalid_credentials"),
            headers={"WWW-Authenticate": "Bearer"},
        )
    session = db.exec(
        select(UserSession).where(
            UserSession.token == token,
            UserSession.is_active == True,  # noqa: E712
        )
    ).first()

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail(request, "auth.invalid_credentials"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    now = now_utc()

    # 绝对过期检查
    if now > ensure_utc(session.expires_at):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail(request, "auth.expired"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 滑动过期检查：超过 N 天未使用则失效
    slide_limit = ensure_utc(session.last_used_at) + timedelta(days=settings.USER_SESSION_SLIDE_DAYS)
    if now > slide_limit:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail(request, "auth.expired"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 滑动窗口更新 last_used_at：60 秒内只写一次，降低并发/E2E 多标签页下的 SQLite 写竞争
    last_used = ensure_utc(session.last_used_at)
    if last_used is None or (now - last_used) > timedelta(seconds=60):
        db.exec(
            update(UserSession)
            .where(UserSession.id == session.id)
            .values(last_used_at=now)
        )
        db.commit()

    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail(request, "auth.invalidated"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def require_admin(
    request: Request, current_user: User = Depends(get_current_user)
) -> User:
    """校验当前用户为 admin，供其他路由依赖注入。"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_detail(request, "auth.admin_required"),
        )
    return current_user


def require_feature(feature: str):
    """生成依赖工厂：校验指定 License 功能是否已解锁。"""

    def _check_feature(request: Request) -> None:
        if not check_feature_enabled(feature):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=error_detail(request, "license.feature_locked", feature=feature),
            )

    return _check_feature


def get_token_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[str]:
    """提取 Token 但不校验，用于登出等需要对无效 Token 友好的场景。

    优先从 cookie 读取，缺失时 fallback 到 Authorization Bearer。
    """
    return _extract_token(request, credentials)


def get_pagination(
    page: int = Query(default=DEFAULT_PAGE, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> PageParams:
    """FastAPI 依赖：解析并校验分页查询参数。"""
    return PageParams(page=page, page_size=page_size)


# ---------------------------------------------------------------------------
# Agent Token 校验缓存（H4：ingest 高并发连接池耗尽修复）
# ---------------------------------------------------------------------------
# 背景：Y3 压测（200 节点 / 目标 50k samples/s）时 ingest 端点每请求查 nodes 表
# 校验 agent_token，高并发下连接争用导致 QueuePool 耗尽。这里对「通过校验的
# token -> 节点字段快照」做进程内短 TTL 缓存，命中即免查库。
#
# 失效语义（务必知悉）：节点软删除 / token 轮换 / token 撤销后最长 60 秒生效。
# 可接受理由：token 泄露场景下 60 秒窗口远小于人工响应时间；多 worker 各自
# 缓存不共享，不引入 Redis。
#
# 安全：token 是 bearer 凭证，缓存仅存内存、日志绝不打印 token 原文。
_AGENT_TOKEN_CACHE_TTL_SECONDS = 60.0
_AGENT_TOKEN_CACHE_MAX_SIZE = 10_000
# token -> (过期时刻 time.monotonic, 节点字段快照 dict)；OrderedDict 按 LRU 排序
_agent_token_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_agent_token_cache_lock = threading.Lock()


def reset_agent_token_cache() -> None:
    """清空 Agent Token 校验缓存（测试隔离用，参照 reset_rate_limiter 模式）。"""
    with _agent_token_cache_lock:
        _agent_token_cache.clear()


def _agent_token_cache_get(token: str) -> Optional[dict]:
    """读取缓存；未命中/过期返回 None（过期条目顺手删除）。命中按 LRU 续到尾部。"""
    now = time.monotonic()
    with _agent_token_cache_lock:
        entry = _agent_token_cache.get(token)
        if entry is None:
            return None
        expires_at, snapshot = entry
        if now >= expires_at:
            _agent_token_cache.pop(token, None)
            return None
        _agent_token_cache.move_to_end(token)
        return snapshot


def _agent_token_cache_put(token: str, node: Node) -> None:
    """写入缓存（全字段快照，供命中时重建瞬时 Node）；超上限先扫过期再 LRU 淘汰。"""
    expires_at = time.monotonic() + _AGENT_TOKEN_CACHE_TTL_SECONDS
    snapshot = node.model_dump()
    with _agent_token_cache_lock:
        _agent_token_cache[token] = (expires_at, snapshot)
        _agent_token_cache.move_to_end(token)
        if len(_agent_token_cache) > _AGENT_TOKEN_CACHE_MAX_SIZE:
            now = time.monotonic()
            expired = [
                k for k, (exp, _) in _agent_token_cache.items() if now >= exp
            ]
            for k in expired:
                _agent_token_cache.pop(k, None)
        while len(_agent_token_cache) > _AGENT_TOKEN_CACHE_MAX_SIZE:
            _agent_token_cache.popitem(last=False)  # 淘汰最久未命中条目


def _fetch_node_by_token(db: Session, token: str) -> Optional[Node]:
    """按 agent_token 查 nodes 表（缓存未命中时的唯一 DB 查询点，便于测试计数）。"""
    return db.exec(
        select(Node).where(
            Node.agent_token == token,
            Node.is_deleted == False,  # noqa: E712
        )
    ).first()


def get_current_node(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> Node:
    """从 Authorization: Bearer <agent_token> 中提取并校验当前节点。

    用于 Agent 请求 Server 时的认证。Token 对应节点必须存在且未被软删除。
    同时校验 Agent Token 是否已被撤销：token_revoked_at 不为空且
    token_issued_at <= token_revoked_at 时拒绝认证。
    注意：错误信息中不得包含原始 Token，也不得将 Token 写入日志。
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail(request, "auth.invalid_credentials"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # H4：进程内短 TTL 缓存命中则免查库（见上方缓存说明）。
    cached_snapshot = _agent_token_cache_get(token)
    if cached_snapshot is not None:
        return Node(**cached_snapshot)

    node = _fetch_node_by_token(db, token)

    if node is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail(request, "auth.invalid_credentials"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Agent Token 撤销检查
    if node.token_revoked_at is not None and node.token_issued_at <= node.token_revoked_at:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail(request, "auth.invalid_credentials"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 仅缓存通过全部校验的节点；失败结果不缓存（401 每次都查库，成本可接受）
    _agent_token_cache_put(token, node)
    return node
