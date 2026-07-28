"""开放 API 认证、scope/RBAC 授权与限流依赖（Phase 3 Step 04）。

安全模型（与 ``docs/api/API_OPEN_DESIGN.md`` 对齐）：

- 认证：``Authorization: Bearer ak_xxx``。数据库只存 ``sha256(明文)``，先用
  明文密钥体前 8 位（prefix）快速缩小候选，再做常量时间哈希比对。
- 授权两层：scope（Key 自身授权范围）+ RBAC（绑定用户的角色）。``write:*``
  操作要求绑定用户为 admin，与 Web 控制台一致；``read:*`` 任意启用用户可读。
- 限流：per ``key_id``，默认窗口 60s。Key 自带 ``rate_limit>0`` 时优先，否则按
  License edition 默认（free60/pro600/ent6000 每分钟）。三态后端（收尾修复第 9 批）：
  Redis 正常→分布式固定窗口（多 worker 共享额度）；Redis 未启用（纯同步模式）→
  进程内滑动窗口、限额不变；Redis 运行期故障（熔断窗口内 / 计数异常）→ 进程内窗口、
  限额按 ``OPEN_API_RATE_LIMIT_DEGRADED_RATIO`` 收紧（宁严勿宽，防多 worker 回退放大）。

失败语义：

- 认证失败（缺/错 Key、过期、Key 禁用、绑定用户禁用）→ 401。
- 当前 License 未解锁开放 API（非企业版/已降级，fail-closed）→ 403。
- scope 不足或 RBAC 不足 → 403。
- 超限 → 429，并带 ``Retry-After`` 响应头。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, select, update

from app.core.config import settings
from app.core.licensing import check_feature_enabled, get_current_edition
from app.core.logger import get_logger
from app.core.utils import ensure_utc, now_utc
from app.api.deps import get_db
from app.models.api_key import ApiKey
from app.models.user import User

logger = get_logger("core.api_auth")

# auto_error=False：由本模块统一决定 401 文案与响应头
_bearer = HTTPBearer(auto_error=False)

# 内置默认限流表（次/分钟）；settings.OPEN_API_DEFAULT_RATE_LIMITS 可覆盖。
_BUILTIN_DEFAULT_RATE_LIMITS: dict[str, int] = {
    "free": 60,
    "professional": 600,
    "enterprise": 6000,
}


@dataclass
class ApiKeyContext:
    """一次开放 API 请求解析出的认证上下文。"""

    api_key: ApiKey
    user: User


# ---------------------------------------------------------------------------
# Key 生成与哈希
# ---------------------------------------------------------------------------


def hash_api_key(plaintext: str) -> str:
    """计算 API Key 明文的 sha256 十六进制摘要。"""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """生成一对新的 API Key。

    Returns:
        (plaintext, key_hash, prefix)：
        - plaintext：``ak_`` + 高熵密钥体，**仅此一次**返回给调用方；
        - key_hash：``sha256(plaintext)``，入库；
        - prefix：密钥体前 8 位，入库用于快速定位候选。
    """
    secret = secrets.token_urlsafe(settings.OPEN_API_KEY_TOKEN_BYTES)
    plaintext = f"ak_{secret}"
    return plaintext, hash_api_key(plaintext), secret[:8]


# ---------------------------------------------------------------------------
# 限流：进程内 per-key 滑动窗口
# ---------------------------------------------------------------------------

_rate_lock = threading.Lock()
_rate_windows: dict[int, deque[float]] = {}

# 限流后端三态（收尾修复第 9 批）
_BACKEND_REDIS = "redis"  # Redis 正常：分布式固定窗口，多 worker 共享额度
_BACKEND_MEMORY = "memory"  # Redis 未启用（纯同步模式）：进程内窗口，限额不变
_BACKEND_MEMORY_DEGRADED = "memory_degraded"  # Redis 运行期故障：进程内窗口，降级限额

# 进入降级限流的告警节流：key_id -> 上次告警的 monotonic 时间。
# Redis 抖动期每个请求都走故障路径，必须节流（每 key 每 30s 至多一条）防刷屏。
_DEGRADED_WARN_INTERVAL_SECONDS = 30.0
_degraded_warn_lock = threading.Lock()
_degraded_warn_last: dict[int, float] = {}

# last_used_at 写入节流：key_id -> 上次写入的 monotonic 时间
_last_used_lock = threading.Lock()
_last_used_written: dict[int, float] = {}


def _default_rate_limit_for_edition() -> int:
    """按当前 License edition 解析默认每分钟限流。"""
    raw = settings.OPEN_API_DEFAULT_RATE_LIMITS
    table: dict[str, int] = dict(_BUILTIN_DEFAULT_RATE_LIMITS)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(k, str) and isinstance(v, (int, float)) and v > 0:
                        table[k] = int(v)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("OPEN_API_DEFAULT_RATE_LIMITS 解析失败，使用内置默认表")
    edition = get_current_edition()
    return table.get(edition, table["free"])


def _resolve_rate_limit(key: ApiKey) -> int:
    """解析某 Key 的每分钟限流：Key 自带 >0 优先，否则按 edition 默认。"""
    if key.rate_limit and key.rate_limit > 0:
        return int(key.rate_limit)
    return _default_rate_limit_for_edition()


def _rate_limit_backend_state() -> str:
    """判定限流后端三态（区分"未启用 Redis"与"Redis 运行期故障"）。

    - 任务队列处于 fallback（``_redis is None``，从未配置 Redis 的官方同步部署形态）
      → ``_BACKEND_MEMORY``：回退进程内窗口，限额保持 limit 不变；
    - 配置了 Redis 但处于运行期熔断窗口（``is_async()`` 为 False）
      → ``_BACKEND_MEMORY_DEGRADED``：故障期，回退窗口限额收紧，宁严勿宽；
    - 其余 → ``_BACKEND_REDIS``：分布式固定窗口。
    """
    from app.core.task_queue import get_task_queue

    queue = get_task_queue()
    if queue.is_fallback:
        return _BACKEND_MEMORY
    if not queue.is_async():
        return _BACKEND_MEMORY_DEGRADED
    return _BACKEND_REDIS


def _warn_degraded_rate_limit(key_id: int, limit: int, degraded_limit: int) -> None:
    """进入降级限流的告警（节流：同一 Key 每 30s 至多一条）。

    Redis 抖动期可能每个请求都走故障路径，逐请求打日志会刷屏并放大故障影响。
    """
    now = time.monotonic()
    with _degraded_warn_lock:
        last = _degraded_warn_last.get(key_id, 0.0)
        if now - last < _DEGRADED_WARN_INTERVAL_SECONDS:
            return
        _degraded_warn_last[key_id] = now
    logger.warning(
        f"开放 API 限流进入故障降级模式（Redis 运行期不可用，宁严勿宽）: "
        f"key_id={key_id} 限额 {limit}→{degraded_limit}"
    )


def _redis_rate_limit(key_id: int, limit: int, window: float) -> Optional[tuple[bool, int]]:
    """分布式固定窗口计数限流（Phase 3 Step 05）。

    仅当任务队列后端为 Redis（多 worker 共享）时启用，返回 (allowed, retry_after)；
    Redis 不可用或运行期异常返回 None，由调用方回退到进程内滑动窗口。

    采用固定窗口计数（key 含时间桶），INCR + EXPIRE 原子完成，可解决多 worker
    下进程内滑动窗口各自计数导致的总额度放大问题。代价是窗口边界可能有轻微突发，
    但配额总量正确，符合限流目的。
    """
    from app.core.task_queue import get_task_queue

    queue = get_task_queue()
    if not queue.is_async():
        return None
    client = queue.get_redis_client()
    if client is None:
        return None

    now = time.time()
    bucket = int(now // window)
    rkey = f"chaosops:ratelimit:{key_id}:{bucket}"
    try:
        pipe = client.pipeline()
        pipe.incr(rkey)
        pipe.expire(rkey, int(window) + 1)
        result = pipe.execute()
        count = int(result[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Redis 限流计数失败，回退内存窗口: key_id={key_id}, error={exc}")
        return None

    if count > limit:
        elapsed_in_window = now - bucket * window
        retry_after = max(1, int(math.ceil(window - elapsed_in_window)))
        return False, retry_after
    return True, 0


def enforce_rate_limit(key: ApiKey) -> None:
    """对指定 Key 执行限流；超限抛 429 并带 Retry-After。

    三态后端（见 ``_rate_limit_backend_state``）：

    - Redis 正常 → 分布式固定窗口计数（多 worker 共享额度）；
    - Redis 未启用（纯同步模式）→ 进程内滑动窗口，限额 = limit（不变）；
    - Redis 运行期故障（熔断窗口内 / 计数异常）→ 进程内滑动窗口，限额收紧为
      ``max(1, int(limit * OPEN_API_RATE_LIMIT_DEGRADED_RATIO))``：回退窗口在各 worker
      内各自计数、总额度会被放大，故障期宁严勿宽。
    """
    limit = _resolve_rate_limit(key)
    window = float(settings.OPEN_API_RATE_LIMIT_WINDOW_SECONDS)

    state = _rate_limit_backend_state()
    if state == _BACKEND_REDIS:
        redis_result = _redis_rate_limit(key.id, limit, window)
        if redis_result is not None:
            allowed, retry_after = redis_result
            if not allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="请求过于频繁，请稍后重试",
                    headers={"Retry-After": str(retry_after)},
                )
            return
        # pipeline 执行异常等运行期故障 → 故障期降级路径
        state = _BACKEND_MEMORY_DEGRADED

    effective_limit = limit
    if state == _BACKEND_MEMORY_DEGRADED:
        ratio = min(1.0, max(0.0, float(settings.OPEN_API_RATE_LIMIT_DEGRADED_RATIO)))
        effective_limit = max(1, int(limit * ratio))
        _warn_degraded_rate_limit(key.id, limit, effective_limit)

    # 回退：进程内滑动窗口（未启用 Redis 时限额不变；故障期用降级限额）
    now = time.monotonic()
    cutoff = now - window

    with _rate_lock:
        dq = _rate_windows.setdefault(key.id, deque())
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= effective_limit:
            oldest = dq[0]
            retry_after = max(1, int(math.ceil(window - (now - oldest))))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="请求过于频繁，请稍后重试",
                headers={"Retry-After": str(retry_after)},
            )
        dq.append(now)


def reset_rate_limiter() -> None:
    """清空限流窗口（测试隔离用）。

    同时清空进程内滑动窗口、降级告警节流状态与（若存在）Redis 限流计数键。
    """
    with _rate_lock:
        _rate_windows.clear()
    with _degraded_warn_lock:
        _degraded_warn_last.clear()
    try:
        from app.core.task_queue import get_task_queue

        client = get_task_queue().get_redis_client()
        if client is not None:
            cursor = 0
            while True:
                cursor, keys = client.scan(
                    cursor=cursor, match="chaosops:ratelimit:*", count=200
                )
                if keys:
                    client.delete(*keys)
                if cursor == 0:
                    break
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 认证依赖
# ---------------------------------------------------------------------------


def _unauthorized(detail: str = "无法验证凭据") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _maybe_touch_last_used(db: Session, key: ApiKey) -> None:
    """节流更新 last_used_at：同一 Key 在配置间隔内最多写库一次。"""
    interval = float(settings.OPEN_API_LAST_USED_WRITE_INTERVAL_SECONDS)
    now_mono = time.monotonic()
    with _last_used_lock:
        last = _last_used_written.get(key.id, 0.0)
        if now_mono - last < interval:
            return
        _last_used_written[key.id] = now_mono
    try:
        db.exec(
            update(ApiKey)
            .where(ApiKey.id == key.id)
            .values(last_used_at=now_utc())
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        # 更新失败不影响认证结果，回滚并记日志
        db.rollback()
        logger.warning(f"更新 API Key last_used_at 失败: key_id={key.id}, error={exc}")


def get_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> ApiKeyContext:
    """解析并校验 ``Authorization: Bearer ak_xxx``，返回认证上下文。

    认证成功后写入 ``request.state.api_key_id`` / ``api_user_id``，供审计中间件
    记录调用；并按 30s 节流更新 ``last_used_at``。
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    token = credentials.credentials
    if not token or not token.startswith("ak_") or len(token) < 11:
        # 最短合法：ak_(3) + 至少 8 位密钥体
        raise _unauthorized()

    secret = token[3:]
    prefix = secret[:8]
    candidate_hash = hash_api_key(token)

    candidates = db.exec(
        select(ApiKey).where(ApiKey.prefix == prefix)
    ).all()

    matched: Optional[ApiKey] = None
    for candidate in candidates:
        if hmac.compare_digest(candidate.key_hash, candidate_hash):
            matched = candidate
            break

    if matched is None:
        raise _unauthorized()

    if not matched.enabled:
        raise _unauthorized("凭据已失效")

    if matched.expires_at is not None and now_utc() > ensure_utc(matched.expires_at):
        raise _unauthorized("凭据已过期")

    user = db.get(User, matched.user_id)
    if user is None or not user.is_active:
        # 绑定用户被禁用 → Key 立即失效（设计 §8 防御性要求）
        raise _unauthorized("凭据已失效")

    # License 门控（fail-closed）：开放 API 为企业版专属功能。
    # License 降级/过期/校验异常时，已签发的 Key 调用开放 API 一律被拒；
    # License 读取本身走进程内缓存（上传时刷新），不引入额外缓存层。
    try:
        open_api_enabled = check_feature_enabled("open_api")
    except Exception:  # noqa: BLE001
        open_api_enabled = False
    if not open_api_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前 License 未解锁功能: open_api",
        )

    _maybe_touch_last_used(db, matched)

    # 供审计中间件读取（不泄露任何敏感信息）
    request.state.api_key_id = matched.id
    request.state.api_user_id = user.id

    return ApiKeyContext(api_key=matched, user=user)


# ---------------------------------------------------------------------------
# 授权依赖：scope + RBAC
# ---------------------------------------------------------------------------


def require_scopes(*required_scopes: str):
    """生成依赖：校验 Key 拥有全部 ``required_scopes``，并按 scope 施加 RBAC。

    - 任一 ``write:*`` scope 要求绑定用户角色为 admin（与 Web 控制台写权限一致）。
    - scope 不足 → 403；RBAC 不足 → 403。
    - 通过校验后执行限流（per key_id）。
    """

    def _checker(ctx: ApiKeyContext = Depends(get_api_key)) -> ApiKeyContext:
        granted = set(ctx.api_key.scopes_list())
        missing = [s for s in required_scopes if s not in granted]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API Key 缺少所需 scope",
            )
        # 写操作要求 admin
        if any(s.startswith("write:") for s in required_scopes) and ctx.user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="需要 admin 权限",
            )
        enforce_rate_limit(ctx.api_key)
        return ctx

    return _checker


def require_api_admin(ctx: ApiKeyContext = Depends(get_api_key)) -> ApiKeyContext:
    """要求开放 API 调用者绑定用户为 admin（用于 webhooks 写操作等）。"""
    if ctx.user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要 admin 权限",
        )
    return ctx
