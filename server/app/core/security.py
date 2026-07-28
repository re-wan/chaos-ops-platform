"""ChaosOps Server 安全工具函数。

包含密码 bcrypt 哈希、Session Token 生成等。
"""

import secrets
from typing import Optional

import bcrypt
from sqlmodel import Session, select

from app.models.user_session import UserSession

# bcrypt rounds：平衡安全性与性能
BCRYPT_ROUNDS = 12

# 用于用户不存在时执行一次虚假 verify，防止时序攻击泄露用户名是否存在
DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def hash_password(password: str) -> str:
    """使用 bcrypt 对明文密码进行哈希。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """校验明文密码与 bcrypt 哈希是否匹配。"""
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


# 已知弱口令清单：初始管理员口令命中即视为高风险（含历史默认值与 env 示例占位串）
_WEAK_ADMIN_PASSWORDS = frozenset(
    {
        "chaosops",
        "123456",
        "12345678",
        "admin",
        "password",
        "qwerty",
        "changeme",
        "change-me-admin-password",
    }
)


def is_weak_admin_password(password: Optional[str]) -> bool:
    """判断初始管理员口令是否为弱口令（命中弱口令表或长度不足 12 位）。"""
    if not password:
        return True
    return password.strip().lower() in _WEAK_ADMIN_PASSWORDS or len(password) < 12


def generate_session_token() -> str:
    """生成密码学安全的 Session Token，前缀 u_，长度不少于 64 字节。"""
    return "u_" + secrets.token_urlsafe(64)


def create_user_session(db: Session, user_id: int, max_age_days: int = 30) -> str:
    """为用户创建新的 Session Token，唯一性冲突时最多重试 3 次。

    Args:
        db: 数据库 Session
        user_id: 用户 ID
        max_age_days: Token 绝对有效期，默认 30 天

    Returns:
        生成的 Token 字符串

    Raises:
        RuntimeError: 连续 3 次生成均冲突
    """
    from datetime import datetime, timedelta, timezone

    for _ in range(3):
        token = generate_session_token()
        existing = db.exec(select(UserSession).where(UserSession.token == token)).first()
        if existing is not None:
            continue

        expires_at = datetime.now(timezone.utc) + timedelta(days=max_age_days)
        session = UserSession(user_id=user_id, token=token, expires_at=expires_at)
        db.add(session)
        db.commit()
        db.refresh(session)
        return token

    raise RuntimeError("无法生成唯一的 Session Token")
