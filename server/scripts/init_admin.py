#!/usr/bin/env python3
"""初始化默认管理员账号。

默认用户名为 admin，可通过环境变量覆盖：
    DEFAULT_ADMIN_USERNAME / DEFAULT_ADMIN_PASSWORD

安全策略（收尾修复第 6 批）：
- 不再内置固定弱口令。未设置 DEFAULT_ADMIN_PASSWORD 时，随机生成一次性初始口令，
  并**仅在首次创建账号时**打印到 stdout（请立即登录后修改）。
- 显式设置但命中弱口令（chaosops/123456/过短等）时打印强警告。

生产环境部署后，请务必修改初始密码。
"""

import secrets
import sys
from pathlib import Path

# 将 backend 目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, select

from app.core.config import settings
from app.core.database import engine, init_db
from app.core.security import hash_password, is_weak_admin_password
from app.models.user import User


def init_admin() -> None:
    """创建默认管理员账号（如果不存在）。"""
    # 确保表已创建
    init_db()

    username = settings.DEFAULT_ADMIN_USERNAME
    password = settings.DEFAULT_ADMIN_PASSWORD
    generated = False
    if not password:
        # 未显式配置 → 随机生成一次性初始口令（24 字符 URL 安全串）
        password = secrets.token_urlsafe(18)
        generated = True

    with Session(engine) as session:
        existing = session.exec(select(User).where(User.username == username)).first()
        if existing is not None:
            # 账号已存在：不重新生成、不再打印任何口令（口令只显示一次）
            print(f"管理员 '{username}' 已存在，跳过创建。")
            return

        user = User(
            username=username,
            hashed_password=hash_password(password),
            is_active=True,
        )
        session.add(user)
        session.commit()
        print(f"管理员 '{username}' 创建成功。")

        if generated:
            print(f"初始管理员随机口令（仅显示一次，请立即登录后修改）: {password}")
        elif is_weak_admin_password(password):
            print(
                "强烈警告：DEFAULT_ADMIN_PASSWORD 为弱口令，任何猜到默认值的人都可登录管理员账号，"
                "请立即修改为高强度随机口令！"
            )


if __name__ == "__main__":
    init_admin()
