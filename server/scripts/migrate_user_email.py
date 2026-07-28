#!/usr/bin/env python3
"""User email 字段迁移脚本。

项目当前未引入 Alembic，本脚本用于在已有数据库上安全地添加/回填 email 字段。
SQLModel 会在下次 Server 启动时自动创建 email 列（SQLite ADD COLUMN 兼容）。

用法：
    # 列出所有未配置邮箱的用户
    python scripts/migrate_user_email.py --list

    # 为指定用户设置邮箱
    python scripts/migrate_user_email.py --username admin --email admin@example.com

    # 批量导入（CSV 格式：username,email）
    python scripts/migrate_user_email.py --csv users.csv
"""

import argparse
import csv
import sys
from pathlib import Path

# 将 backend 目录加入路径，以便导入 app 包
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from sqlmodel import Session, select

from app.core.database import engine
from app.models.user import User


def list_users_without_email(session: Session) -> list[User]:
    """列出所有 email 为空的用户。"""
    return session.exec(select(User).where(User.email == None)).all()  # noqa: E711


def set_user_email(session: Session, username: str, email: str) -> bool:
    """为指定用户设置邮箱，返回是否成功。"""
    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        print(f"用户不存在: {username}")
        return False

    existing = session.exec(select(User).where(User.email == email, User.id != user.id)).first()
    if existing is not None:
        print(f"邮箱已被其他用户使用: {email}")
        return False

    user.email = email
    session.add(user)
    session.commit()
    print(f"已更新用户邮箱: {username} -> {email}")
    return True


def import_from_csv(session: Session, csv_path: Path) -> tuple[int, int]:
    """从 CSV 批量导入邮箱，返回 (成功数, 失败数)。"""
    success = 0
    failed = 0
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].startswith("#"):
                continue
            if len(row) < 2:
                print(f"跳过无效行: {row}")
                failed += 1
                continue
            username, email = row[0].strip(), row[1].strip()
            if set_user_email(session, username, email):
                success += 1
            else:
                failed += 1
    return success, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="User email 迁移工具")
    parser.add_argument("--list", action="store_true", help="列出未配置邮箱的用户")
    parser.add_argument("--username", help="目标用户名")
    parser.add_argument("--email", help="要设置的邮箱")
    parser.add_argument("--csv", type=Path, help="批量导入 CSV 文件路径")
    args = parser.parse_args()

    with Session(engine) as session:
        if args.list:
            users = list_users_without_email(session)
            if not users:
                print("所有用户均已配置邮箱")
                return 0
            print("未配置邮箱的用户：")
            for user in users:
                print(f"  - {user.username} (id={user.id})")
            return 0

        if args.username and args.email:
            return 0 if set_user_email(session, args.username, args.email) else 1

        if args.csv:
            success, failed = import_from_csv(session, args.csv)
            print(f"导入完成: 成功 {success}, 失败 {failed}")
            return 0 if failed == 0 else 1

        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
