"""告警通知聚合（初步实现）。

MVP 阶段提供聚合函数，将多个告警合并为一条消息；
实际与通知渠道集成在 Step 17/18 完成。
"""

from app.core.config import settings


def aggregate_notifications(
    alerts: list[dict],
    max_bytes: int = 4096,
) -> str:
    """将多条告警聚合为一条通知消息。

    Args:
        alerts: 告警列表，每项至少包含 rule_id、node_id、severity、message。
        max_bytes: 单条消息大小上限（字节）。

    Returns:
        合并后的消息内容，超过上限时截断并提示。
    """
    if not alerts:
        return "暂无告警"

    header = f"【ChaosOps 聚合告警】共 {len(alerts)} 条\n"
    lines: list[str] = []
    for alert in alerts:
        line = (
            f"- [{alert.get('severity', 'unknown')}] "
            f"{alert.get('message', '无标题')} "
            f"(rule={alert.get('rule_id')}, node={alert.get('node_id')})"
        )
        lines.append(line)

    body = "\n".join(lines)
    footer = "\n请登录控制台查看详情。"
    message = header + body + footer

    encoded = message.encode("utf-8")
    if len(encoded) > max_bytes:
        truncated = encoded[: max_bytes - len("\n...（消息过长，已截断）".encode("utf-8"))]
        message = truncated.decode("utf-8", errors="ignore")
        message += "\n...（消息过长，已截断）"

    return message


def get_aggregate_max_bytes() -> int:
    """返回通知聚合单条消息大小上限。"""
    return getattr(settings, "ALERT_AGGREGATE_MAX_BYTES", 4096)
