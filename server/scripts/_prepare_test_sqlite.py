#!/usr/bin/env python3
"""准备一个用于 PostgreSQL 迁移测试的 SQLite 数据库。

用法：
    python backend/scripts/_prepare_test_sqlite.py --output /tmp/test_chaosops.db

说明：
- 创建用户、节点、告警规则、事件、自愈任务、通知渠道等测试数据。
- 输出 SQLite 数据库文件路径，供 migrate_to_postgres.py 使用。
"""

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from app.core.security import hash_password
from app.core.utils import now_utc
from app.models.alert_rule import AlertRule
from app.models.heal_task import HealTask
from app.models.incident import Incident
from app.models.notification_channel import NotificationChannel
from app.models.user import User
from app.services.node_service import create_node


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="准备 PostgreSQL 迁移测试用的 SQLite 数据")
    parser.add_argument(
        "--output",
        default="/tmp/test_chaosops.db",
        help="输出 SQLite 数据库文件路径",
    )
    return parser.parse_args()


def prepare_database(db_path: str) -> None:
    """创建 SQLite 数据库并插入测试数据。"""
    # 删除旧文件，确保可重复运行
    Path(db_path).unlink(missing_ok=True)

    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        # 1. 用户
        users = [
            User(
                username="admin",
                email="admin@chaosops.local",
                hashed_password=hash_password("admin123"),
                is_active=True,
            ),
            User(
                username="operator",
                email="operator@chaosops.local",
                hashed_password=hash_password("op123456"),
                is_active=True,
            ),
        ]
        session.add_all(users)
        session.flush()

        # 2. 节点（使用 service，自动生成 token）
        nodes = []
        for i in range(3):
            node = create_node(
                session,
                name=f"test-node-{i}",
                host=f"192.168.1.{10 + i}",
                description=f"测试节点 {i}",
                platform="linux",
                group="web-server" if i < 2 else "db-server",
                labels={"env": "test", "index": str(i)},
            )
            nodes.append(node)
        session.flush()

        # 3. 通知渠道
        channels = [
            NotificationChannel(
                name="测试邮件组",
                channel_type="email",
                config=json.dumps(
                    {"recipients": ["ops@chaosops.local"]}, ensure_ascii=False
                ),
                enabled=True,
            ),
            NotificationChannel(
                name="测试 Webhook",
                channel_type="webhook",
                config=json.dumps(
                    {"url": "https://example.com/webhook"}, ensure_ascii=False
                ),
                enabled=True,
            ),
        ]
        session.add_all(channels)
        session.flush()

        # 4. 告警规则
        rules = []
        for i in range(3):
            rule = AlertRule(
                name=f"test-rule-{i}",
                description=f"测试告警规则 {i}",
                scope="global",
                condition_type="json_dsl",
                condition=json.dumps(
                    {"metric": "cpu", "op": ">", "value": 80 + i}, ensure_ascii=False
                ),
                pending_duration_seconds=60,
                resolve_duration_seconds=60,
                severity="warning" if i == 0 else "critical",
                enabled=True,
                notification_channel_ids=json.dumps([c.id for c in channels]),
            )
            rules.append(rule)
        session.add_all(rules)
        session.flush()

        # 5. 事件（Incident）
        incidents = []
        for i, rule in enumerate(rules):
            incident = Incident(
                title=f"测试事件 {i}",
                description=f"由规则 {rule.name} 触发",
                severity=rule.severity,
                status="open" if i == 0 else "resolved",
                source="auto",
                rule_id=rule.id,
                node_id=nodes[i % len(nodes)].node_id,
                started_at=now_utc() - timedelta(hours=i + 1),
                resolved_at=now_utc() if i > 0 else None,
            )
            incidents.append(incident)
        session.add_all(incidents)
        session.flush()

        # 6. 自愈任务
        heal_tasks = [
            HealTask(
                node_id=nodes[0].node_id,
                alert_rule_id=rules[0].id,
                heal_rule_id=1,
                action_id="restart_service",
                action_params=json.dumps({"service": "nginx"}, ensure_ascii=False),
                status="success",
                risk_level="medium",
                requires_approval=False,
                executed_by="agent_test",
                started_at=now_utc() - timedelta(minutes=10),
                finished_at=now_utc() - timedelta(minutes=9),
                result=json.dumps({"exit_code": 0}, ensure_ascii=False),
            ),
            HealTask(
                node_id=nodes[1].node_id,
                alert_rule_id=rules[1].id,
                heal_rule_id=1,
                action_id="clear_disk",
                action_params=json.dumps({"path": "/tmp"}, ensure_ascii=False),
                status="pending",
                risk_level="high",
                requires_approval=True,
            ),
        ]
        session.add_all(heal_tasks)

        session.commit()

        print(f"测试数据库已创建: {db_path}")
        print(f"  用户: {len(users)}")
        print(f"  节点: {len(nodes)}")
        print(f"  通知渠道: {len(channels)}")
        print(f"  告警规则: {len(rules)}")
        print(f"  事件: {len(incidents)}")
        print(f"  自愈任务: {len(heal_tasks)}")


if __name__ == "__main__":
    args = parse_args()
    prepare_database(args.output)
