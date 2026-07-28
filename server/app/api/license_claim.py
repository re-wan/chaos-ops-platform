"""License 自助生成 API（免登录）。

客户付款后凭「订单号 + 购买邮箱 + install_id」自助生成并下载绑定 License。
安全规格（规格书 §4，P0）：

- 限流：每 IP 每 10 分钟最多 5 次验证尝试（滑动窗口，内存计数器）；
- 指数退避：连续失败后下次允许时间递增（1s→2s→4s→8s→16s→30s 封顶）；
- 统一错误：验证失败只返回「订单号、邮箱或 install_id 不匹配」，不区分原因；
- 生成次数硬上限：license_claims.order_id 唯一约束，第 2 次返回「已生成过」；
- 私钥隔离：只返回签名后的文件内容，私钥不出服务端；
- 输入校验：Pydantic 正则白名单，特殊字符 422 拒绝；
- 人机验证：配置 TURNSTILE_SECRET_KEY 时校验 Turnstile；未配置时降级为
  「同一 IP 两次提交至少间隔 3 秒」；
- 审计日志：每次尝试记录时间、IP、脱敏订单号、结果与内部失败原因。
"""

import hmac
import json
import secrets
import threading
import time
from datetime import timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlmodel import Session, select

from app.api.deps import get_db
from app.core.config import settings
from app.core.error_messages import error_detail
from app.core.logger import get_logger
from app.core.mailer import send_license_email
from app.core.utils import ensure_utc, now_utc
from app.models.license_claim import LicenseClaim
from app.services import license_issuer, order_verifier, paddle_client

logger = get_logger("api.license_claim")
audit_logger = get_logger("license_claim.audit")

router = APIRouter(prefix="/api/v1/license", tags=["license-claim"])

# ---- 限流与指数退避（内存滑动窗口，规格 §4.1） ----

_RATE_LIMIT_MAX = 5  # 每窗口最多尝试次数
_RATE_LIMIT_WINDOW_SECONDS = 600.0  # 10 分钟滑动窗口
_BACKOFF_MAX_SECONDS = 30.0  # 退避封顶


class _IpRateLimiter:
    """每 IP 滑动窗口限流 + 连续失败指数退避（进程内内存计数器）。

    退避通过「响应前延迟」实现（规格 §4.1：连续失败后延迟递增）：
    第 n 次连续失败后，下次请求的响应前延迟 min(30, 2**(n-1)) 秒。
    """

    def __init__(self, now_func=time.monotonic):
        self._lock = threading.Lock()
        self._now = now_func
        # ip -> 窗口内尝试时间戳列表
        self._attempts: dict[str, list[float]] = {}
        # ip -> 连续失败次数
        self._failures: dict[str, int] = {}
        # ip -> 上次提交时间（Turnstile 未配置时的 3 秒降级间隔）
        self._last_submit: dict[str, float] = {}

    def check(self, ip: str) -> bool:
        """检查滑动窗口配额是否允许本次尝试（不计数）。"""
        now = self._now()
        with self._lock:
            attempts = self._attempts.get(ip, [])
            attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW_SECONDS]
            self._attempts[ip] = attempts
            return len(attempts) < _RATE_LIMIT_MAX

    def current_delay(self, ip: str) -> float:
        """本次请求响应前应施加的退避延迟（秒），基于连续失败次数。"""
        with self._lock:
            failures = self._failures.get(ip, 0)
        if failures <= 0:
            return 0.0
        return min(_BACKOFF_MAX_SECONDS, 2.0 ** (failures - 1))

    def check_min_interval(self, ip: str, interval: float) -> bool:
        """Turnstile 降级：同一 IP 两次提交至少间隔 interval 秒。"""
        now = self._now()
        with self._lock:
            return now - self._last_submit.get(ip, -interval) >= interval

    def record_attempt(self, ip: str) -> None:
        """记录一次尝试（占用窗口配额）。"""
        now = self._now()
        with self._lock:
            attempts = self._attempts.setdefault(ip, [])
            attempts.append(now)
            self._last_submit[ip] = now

    def record_failure(self, ip: str) -> None:
        """记录一次验证失败（连续失败计数 +1，下次退避延迟翻倍）。"""
        with self._lock:
            self._failures[ip] = self._failures.get(ip, 0) + 1

    def record_success(self, ip: str) -> None:
        """验证成功：清零连续失败计数。"""
        with self._lock:
            self._failures.pop(ip, None)


_limiter = _IpRateLimiter()
# 退避延迟的 sleep 入口（测试可 monkeypatch 为无操作，避免真实等待）
_sleep = time.sleep


def reset_license_claim_limiter() -> None:
    """重置限流状态（测试隔离用）。"""
    global _limiter
    _limiter = _IpRateLimiter()


def _mask_order_id(order_id: str) -> str:
    """订单号脱敏：仅保留后 4 位（规格 §4.6）。"""
    return f"***{order_id[-4:]}" if len(order_id) >= 4 else "***"


def _audit(ip: str, order_id: str, result: str, reason: str) -> None:
    """审计日志：时间、IP、脱敏订单号、结果、内部失败原因（不暴露给前端）。"""
    audit_logger.info(
        f"license_claim ip={ip} order={_mask_order_id(order_id)} "
        f"result={result} reason={reason}"
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class LicenseClaimRequest(BaseModel):
    """自助生成请求（输入校验规格 §4.4：正则白名单，特殊字符 422）。"""

    order_id: str = Field(
        min_length=5, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    email: EmailStr = Field(max_length=254)
    install_id: str = Field(pattern=r"^ins_[a-zA-Z0-9]{16,64}$", max_length=68)
    turnstile_token: str | None = Field(default=None, max_length=2048)


class LicenseClaimResponse(BaseModel):
    license_id: str
    download_url: str
    message: str


def _verify_turnstile(token: str, ip: str) -> bool:
    """校验 Cloudflare Turnstile token。未配置 secret 时不应调用本函数。"""
    try:
        response = httpx.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret": settings.TURNSTILE_SECRET_KEY,
                "response": token,
                "remoteip": ip,
            },
            timeout=10.0,
        )
        return bool(response.json().get("success"))
    except Exception as exc:  # noqa: BLE001
        # 人机验证通道故障时 fail-closed（不放行），但记录日志
        logger.warning(f"Turnstile 校验请求失败（按不通过处理）: {exc}")
        return False


@router.post("/claim", response_model=LicenseClaimResponse)
def claim_license(
    payload: LicenseClaimRequest,
    request: Request,
    session: Session = Depends(get_db),
):
    """自助生成 License：核验订单 → 签发 → 落库 → 邮件发送 → 返回下载链接。"""
    ip = _client_ip(request)

    # 1) 限流（滑动窗口）——每 IP 每 10 分钟最多 5 次尝试
    if not _limiter.check(ip):
        _audit(ip, payload.order_id, "rate_limited", "超出滑动窗口配额")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=error_detail(request, "license_claim.rate_limited"),
        )

    # 2) 人机验证：配置 Turnstile 时强制校验；未配置时降级为 3 秒最小间隔
    if settings.TURNSTILE_SECRET_KEY:
        if not payload.turnstile_token or not _verify_turnstile(
            payload.turnstile_token, ip
        ):
            _limiter.record_attempt(ip)
            _limiter.record_failure(ip)
            _audit(ip, payload.order_id, "failure", "Turnstile 校验未通过")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=error_detail(request, "license_claim.mismatch"),
            )
    elif not _limiter.check_min_interval(
        ip, settings.LICENSE_CLAIM_MIN_INTERVAL_SECONDS
    ):
        _audit(ip, payload.order_id, "rate_limited", "低于最小提交间隔（降级人机验证）")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=error_detail(request, "license_claim.rate_limited"),
        )

    # 占用一次窗口配额（此后无论成败均计入）
    _limiter.record_attempt(ip)

    # 3) 指数退避：连续失败后响应前延迟递增（1s→2s→4s→8s→16s→30s 封顶）
    delay = _limiter.current_delay(ip)
    if delay > 0:
        _sleep(delay)

    # 4) 生成次数硬上限：同订单号第 2 次 → 「已生成过，请联系客服」（规格 §4.2）
    existing = session.exec(
        select(LicenseClaim).where(LicenseClaim.order_id == payload.order_id)
    ).first()
    if existing is not None:
        _audit(ip, payload.order_id, "duplicate", "订单已生成过 License")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail(request, "license_claim.already_claimed"),
        )

    # 5) 订单核验：本地人工订单优先，Paddle 自动收款兜底
    #    （核验通道不可用时优雅降级：不生成、不暴露攻击面）
    try:
        order_ok = order_verifier.verify_order(payload.order_id, payload.email)
    except paddle_client.PaddleUnavailableError as exc:
        _audit(ip, payload.order_id, "unavailable", f"订单核验暂不可用: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_detail(request, "license_claim.verify_unavailable"),
        ) from exc

    if not order_ok:
        # 统一错误（规格 §4.1）：绝不区分订单不存在/邮箱不匹配/install_id 问题
        _limiter.record_failure(ip)
        _audit(ip, payload.order_id, "failure", "订单核验未通过（统一错误）")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_detail(request, "license_claim.mismatch"),
        )

    # 6) 签发 License（私钥仅服务端持有，只输出签名后的文件内容）
    license_id = f"lic_{secrets.token_hex(12)}"
    try:
        license_payload = license_issuer.issue_license(
            license_id=license_id, install_id=payload.install_id
        )
    except license_issuer.LicenseIssuerError as exc:
        _audit(ip, payload.order_id, "error", f"签发失败: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_detail(request, "license_claim.issue_failed"),
        ) from exc

    # 7) 落库（order_id 唯一约束兜底并发双请求，规格 §8）
    download_token = secrets.token_urlsafe(32)
    claim = LicenseClaim(
        order_id=payload.order_id,
        email=payload.email,
        install_id=payload.install_id,
        license_id=license_id,
        download_token=download_token,
        token_expires_at=now_utc()
        + timedelta(hours=settings.LICENSE_DOWNLOAD_TOKEN_TTL_HOURS),
        license_json=json.dumps(license_payload, ensure_ascii=False, indent=2),
        ip_address=ip,
    )
    try:
        session.add(claim)
        session.commit()
        session.refresh(claim)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        # 并发下同订单双请求：唯一约束兜底，后到的返回「已生成过」
        _audit(ip, payload.order_id, "duplicate", f"落库冲突（并发兜底）: {exc}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail(request, "license_claim.already_claimed"),
        ) from exc

    # 8) 邮件发送一份（失败不阻断：页面下载链接仍可用）
    try:
        send_license_email(payload.email, license_id, claim.license_json)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"License 邮件发送失败（不阻断下载）: {exc}")

    _limiter.record_success(ip)
    _audit(ip, payload.order_id, "success", f"已生成 {license_id}")
    return LicenseClaimResponse(
        license_id=license_id,
        download_url=(
            f"/api/v1/license/download/{license_id}?token={download_token}"
        ),
        message=error_detail(request, "license_claim.success"),
    )


@router.get("/download/{license_id}")
def download_license(
    license_id: str,
    token: str,
    request: Request,
    session: Session = Depends(get_db),
):
    """下载 License 文件（一次性令牌：24h 有效、单次使用）。"""
    claim = session.exec(
        select(LicenseClaim).where(LicenseClaim.license_id == license_id)
    ).first()
    invalid = (
        claim is None
        or claim.token_used
        or not hmac.compare_digest(claim.download_token, token)
        or ensure_utc(claim.token_expires_at) <= now_utc()
    )
    if invalid:
        _audit(_client_ip(request), license_id, "download_denied", "令牌无效/过期/已使用")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_detail(request, "license_claim.download_invalid"),
        )

    claim.token_used = True  # 单次使用
    session.add(claim)
    session.commit()
    _audit(_client_ip(request), license_id, "download_ok", "License 已下载")
    return Response(
        content=claim.license_json,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="chaosops-license-{license_id}.json"'
            )
        },
    )
