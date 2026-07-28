"""管理员认证相关 API 路由。"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from sqlmodel import Session, select, update

from app.core.error_messages import error_detail
from app.api.deps import get_current_user, get_db, get_token_optional
from app.core.config import settings
from app.core.logger import get_logger
from app.core.mailer import build_password_reset_url, send_password_reset_email
from app.core.security import (
    DUMMY_HASH,
    create_user_session,
    hash_password,
    verify_password,
)
from app.models.password_reset_token import PasswordResetToken
from app.models.user import User
from app.models.user_session import UserSession
from app.schemas.password_reset import (
    PasswordResetConfirm,
    PasswordResetRequest,
    PasswordResetResponse,
)
from app.services import audit_log

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
logger = get_logger("api.auth")


@router.post("/login")
def login(
    request: Request,
    response: Response,
    username: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
) -> dict:
    """管理员登录接口。

    成功后创建服务端 Session Token，并通过 httpOnly cookie 返回，
    同时保留 JSON body 中的 access_token 供脚本/Agent 兼容使用。
    失败时统一返回 401，不暴露用户名是否存在。
    """
    # 非空校验
    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail(request, "auth.bad_credentials"),
        )

    user = db.exec(select(User).where(User.username == username)).first()

    if user is None:
        # 虚假 verify，防止时序攻击泄露用户名是否存在
        verify_password(password, DUMMY_HASH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail(request, "auth.bad_credentials"),
        )

    if not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail(request, "auth.bad_credentials"),
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_detail(request, "auth.account_disabled"),
        )

    token = create_user_session(db, user.id, max_age_days=settings.USER_SESSION_MAX_AGE_DAYS)
    expires_in = int(timedelta(days=settings.USER_SESSION_SLIDE_DAYS).total_seconds())

    # Web 控制台通过 httpOnly cookie 鉴权，防御 XSS 窃取 Token
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        max_age=expires_in,
        path="/",
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expires_in,
    }


@router.post("/logout")
def logout(
    response: Response,
    token: Optional[str] = Depends(get_token_optional),
    db: Session = Depends(get_db),
) -> dict:
    """登出接口。

    找到当前 Token 并将其标记为失效，同时清除 httpOnly cookie；
    对无效 Token 也返回成功，避免泄露该 Token 是否曾经存在。
    """
    if token:
        session = db.exec(select(UserSession).where(UserSession.token == token)).first()
        if session is not None and session.is_active:
            session.is_active = False
            db.add(session)
            db.commit()

    response.delete_cookie(key="access_token", path="/")
    return {"message": "登出成功"}


@router.get("/me")
def me(current_user: User = Depends(get_current_user)) -> dict:
    """获取当前登录管理员信息。"""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "is_active": current_user.is_active,
        "role": current_user.role,
        "created_at": current_user.created_at.isoformat().replace("+00:00", "Z"),
    }


# ---- 密码重置 ----


_PASSWORD_RESET_TOKEN_TTL_MINUTES = 15
_PASSWORD_RESET_RATE_LIMIT_WINDOW_HOURS = 1
_PASSWORD_RESET_RATE_LIMIT_MAX = 3


def _generate_password_reset_token() -> str:
    """生成密码重置令牌，前缀 pr_。"""
    return "pr_" + secrets.token_urlsafe(32)


def _create_password_reset_token(session: Session, user_id: int) -> str:
    """为用户创建新的密码重置令牌，唯一性冲突时最多重试 3 次。"""
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=_PASSWORD_RESET_TOKEN_TTL_MINUTES)

    for _ in range(3):
        token = _generate_password_reset_token()
        existing = session.exec(
            select(PasswordResetToken).where(PasswordResetToken.token == token)
        ).first()
        if existing is not None:
            continue

        reset_token = PasswordResetToken(
            user_id=user_id,
            token=token,
            expires_at=expires_at,
        )
        session.add(reset_token)
        session.commit()
        session.refresh(reset_token)
        return token

    raise RuntimeError("无法生成唯一的密码重置令牌")


def _count_recent_unused_tokens(session: Session, user_id: int) -> int:
    """统计指定用户最近 1 小时内未使用且未过期的重置令牌数量。"""
    since = datetime.now(timezone.utc) - timedelta(hours=_PASSWORD_RESET_RATE_LIMIT_WINDOW_HOURS)
    tokens = session.exec(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used == False,  # noqa: E712
            PasswordResetToken.created_at >= since,
        )
    ).all()
    return len(tokens)


def _deactivate_user_sessions(session: Session, user_id: int) -> None:
    """将指定用户的所有 Session Token 标记为失效。"""
    session.exec(
        update(UserSession)
        .where(UserSession.user_id == user_id)
        .values(is_active=False)
    )
    session.commit()


@router.post(
    "/password-reset/request",
    response_model=PasswordResetResponse,
    status_code=status.HTTP_200_OK,
)
def request_password_reset(
    body: PasswordResetRequest,
    db: Session = Depends(get_db),
) -> dict:
    """请求密码重置。

    无论用户是否存在，都返回统一信息，防止用户枚举。
    """
    user = db.exec(select(User).where(User.username == body.username)).first()

    if user is not None:
        # 频率限制：最近 1 小时内未使用且未过期的令牌不超过 3 个
        if _count_recent_unused_tokens(db, user.id) < _PASSWORD_RESET_RATE_LIMIT_MAX:
            token = _create_password_reset_token(db, user.id)
            reset_url = build_password_reset_url(token)
            # 邮件发送失败不影响接口返回统一信息，但会记录日志
            if user.email:
                send_password_reset_email(to_addr=user.email, reset_url=reset_url)
            else:
                logger.warning(
                    f"用户未配置邮箱，无法发送密码重置邮件: user_id={user.id}"
                )

    return {
        "message": "如果该账号存在且已配置邮箱，重置邮件已发送",
    }


@router.post(
    "/password-reset/confirm",
    response_model=PasswordResetResponse,
    status_code=status.HTTP_200_OK,
)
def confirm_password_reset(
    request: Request,
    body: PasswordResetConfirm,
    db: Session = Depends(get_db),
) -> dict:
    """使用 token 重置密码。

    token 必须存在、未使用、未过期，否则返回统一错误。
    """
    now = datetime.now(timezone.utc)
    reset_token = db.exec(
        select(PasswordResetToken).where(
            PasswordResetToken.token == body.token,
            PasswordResetToken.used == False,  # noqa: E712
            PasswordResetToken.expires_at > now,
        )
    ).first()

    if reset_token is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "auth.reset_token_invalid"),
        )

    user = db.get(User, reset_token.user_id)
    if user is None:
        # 理论上不会发生，因为外键约束保证用户存在
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "auth.reset_token_invalid"),
        )

    user.hashed_password = hash_password(body.new_password)
    reset_token.used = True
    reset_token.used_at = now
    db.add(user)
    db.add(reset_token)
    db.commit()

    # 重置成功后使该用户所有 Session 失效
    _deactivate_user_sessions(db, user.id)

    # 记录审计日志，不记录新密码
    audit_log.record_password_reset(db, user, created_by=user.id)

    return {"message": "密码已重置"}
