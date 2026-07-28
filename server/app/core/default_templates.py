"""系统默认通知模板定义（中英两套）。

Step 18 在 Server 启动时将这些模板初始化到数据库。
模板面向邮件/钉钉等接收端，按部署环境变量 ``DEFAULT_LOCALE``（默认 zh）
选择 seed 哪一套；初始化是 insert-only，存量模板不会被刷新。
"""

from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from app.core.config import settings
from app.core.logger import get_logger
from app.models.notification_template import NotificationTemplate

logger = get_logger("core.default_templates")


def _im_templates(locale: str = "zh") -> list[dict[str, Any]]:
    """为所有 IM 渠道生成默认模板。

    复用 Markdown 内容，标题由 subject_template 提供，
    各后端在 send 中根据 title/message/severity 组装平台请求体。
    """
    im_types = ["dingtalk", "wecom", "lark", "slack"]
    templates: list[dict[str, Any]] = []

    if locale.lower().startswith("zh"):
        event_defs = [
            {
                "event_type": "alert.firing",
                "name_prefix": "告警触发",
                "subject": "[{{ alert.severity | upper }}] 告警触发：{{ alert.rule_name }}",
                "body": (
                    "🚨 **{{ alert.severity | upper }}** 告警触发\n\n"
                    "- 规则：{{ alert.rule_name }}\n"
                    "- 节点：{{ alert.node_name }} ({{ alert.node_id }})\n"
                    "- 指标：{{ alert.metric }} = {{ alert.value }}\n"
                    "- 时间：{{ timestamp }}\n\n"
                    "{% if dashboard_url %}[查看详情]({{ dashboard_url }}){% endif %}"
                ),
            },
            {
                "event_type": "alert.resolved",
                "name_prefix": "告警恢复",
                "subject": "[恢复] 告警恢复：{{ alert.rule_name }}",
                "body": (
                    "✅ **告警恢复**\n\n"
                    "- 规则：{{ alert.rule_name }}\n"
                    "- 节点：{{ alert.node_name }} ({{ alert.node_id }})\n"
                    "- 时间：{{ timestamp }}\n\n"
                    "{% if dashboard_url %}[查看详情]({{ dashboard_url }}){% endif %}"
                ),
            },
            {
                "event_type": "incident.created",
                "name_prefix": "事件创建",
                "subject": "[事件] {{ incident.title }}",
                "body": (
                    "📌 **事件创建**\n\n"
                    "- 标题：{{ incident.title }}\n"
                    "- 状态：{{ incident.status }}\n"
                    "- 时间：{{ timestamp }}\n\n"
                    "{% if dashboard_url %}[查看详情]({{ dashboard_url }}){% endif %}"
                ),
            },
            {
                "event_type": "incident.resolved",
                "name_prefix": "事件解决",
                "subject": "[已解决] {{ incident.title }}",
                "body": (
                    "✅ **事件已解决**\n\n"
                    "- 标题：{{ incident.title }}\n"
                    "- 事件 ID：{{ incident.id }}\n"
                    "- 解决时间：{{ timestamp }}\n\n"
                    "{% if dashboard_url %}[查看详情]({{ dashboard_url }}){% endif %}"
                ),
            },
        ]
    else:
        event_defs = [
            {
                "event_type": "alert.firing",
                "name_prefix": "Alert Firing",
                "subject": "[{{ alert.severity | upper }}] Alert Firing: {{ alert.rule_name }}",
                "body": (
                    "🚨 **{{ alert.severity | upper }}** Alert Firing\n\n"
                    "- Rule: {{ alert.rule_name }}\n"
                    "- Node: {{ alert.node_name }} ({{ alert.node_id }})\n"
                    "- Metric: {{ alert.metric }} = {{ alert.value }}\n"
                    "- Time: {{ timestamp }}\n\n"
                    "[View Details]({{ dashboard_url }})"
                ),
            },
            {
                "event_type": "alert.resolved",
                "name_prefix": "Alert Resolved",
                "subject": "[Resolved] Alert Resolved: {{ alert.rule_name }}",
                "body": (
                    "✅ **Alert Resolved**\n\n"
                    "- Rule: {{ alert.rule_name }}\n"
                    "- Node: {{ alert.node_name }} ({{ alert.node_id }})\n"
                    "- Time: {{ timestamp }}\n\n"
                    "[View Details]({{ dashboard_url }})"
                ),
            },
            {
                "event_type": "incident.created",
                "name_prefix": "Incident Created",
                "subject": "[Incident] {{ incident.title }}",
                "body": (
                    "📌 **Incident Created**\n\n"
                    "- Title: {{ incident.title }}\n"
                    "- Status: {{ incident.status }}\n"
                    "- Time: {{ timestamp }}\n\n"
                    "[View Details]({{ dashboard_url }})"
                ),
            },
            {
                "event_type": "incident.resolved",
                "name_prefix": "Incident Resolved",
                "subject": "[Resolved] {{ incident.title }}",
                "body": (
                    "✅ **Incident Resolved**\n\n"
                    "- Title: {{ incident.title }}\n"
                    "- Incident ID: {{ incident.id }}\n"
                    "- Resolved At: {{ timestamp }}\n\n"
                    "[View Details]({{ dashboard_url }})"
                ),
            },
        ]

    for im_type in im_types:
        for event in event_defs:
            templates.append(
                {
                    "name": f"{event['name_prefix']} - {im_type.upper()}",
                    "event_type": event["event_type"],
                    "channel_type": im_type,
                    "subject_template": event["subject"],
                    "body_template": event["body"],
                    "format": "markdown",
                }
            )
    return templates

# Email/Webhook 基础模板（中文）
_BASE_TEMPLATES_ZH: list[dict[str, Any]] = [
    {
        "name": "告警触发 - Email",
        "event_type": "alert.firing",
        "channel_type": "email",
        "subject_template": "[{{ alert.severity | upper }}] 告警触发：{{ alert.rule_name }}",
        "body_template": (
            "🚨 **{{ alert.severity | upper }}** 告警触发\n\n"
            "- 规则：{{ alert.rule_name }}\n"
            "- 节点：{{ alert.node_name }} ({{ alert.node_id }})\n"
            "- 指标：{{ alert.metric }} = {{ alert.value }}\n"
            "- 时间：{{ timestamp }}\n\n"
            "{% if dashboard_url %}[查看详情]({{ dashboard_url }}){% endif %}"
        ),
        "format": "markdown",
    },
    {
        "name": "告警触发 - Webhook",
        "event_type": "alert.firing",
        "channel_type": "webhook",
        "subject_template": None,
        "body_template": (
            "{\n"
            '  "event_type": "{{ event_type }}",\n'
            '  "severity": "{{ alert.severity }}",\n'
            '  "title": "{{ alert.rule_name }}",\n'
            '  "message": "节点 {{ alert.node_name }} 触发告警",\n'
            '  "metric": "{{ alert.metric }}",\n'
            '  "value": "{{ alert.value }}",\n'
            '  "node_id": "{{ alert.node_id }}",\n'
            '  "timestamp": "{{ timestamp }}",\n'
            '  "dashboard_url": "{{ dashboard_url }}"\n'
            "}"
        ),
        "format": "text",
    },
    {
        "name": "告警恢复 - Email",
        "event_type": "alert.resolved",
        "channel_type": "email",
        "subject_template": "[恢复] 告警恢复：{{ alert.rule_name }}",
        "body_template": (
            "✅ **告警恢复**\n\n"
            "- 规则：{{ alert.rule_name }}\n"
            "- 节点：{{ alert.node_name }} ({{ alert.node_id }})\n"
            "- 时间：{{ timestamp }}\n\n"
            "{% if dashboard_url %}[查看详情]({{ dashboard_url }}){% endif %}"
        ),
        "format": "markdown",
    },
    {
        "name": "告警恢复 - Webhook",
        "event_type": "alert.resolved",
        "channel_type": "webhook",
        "subject_template": None,
        "body_template": (
            "{\n"
            '  "event_type": "{{ event_type }}",\n'
            '  "severity": "{{ alert.severity }}",\n'
            '  "title": "告警恢复：{{ alert.rule_name }}",\n'
            '  "message": "节点 {{ alert.node_name }} 告警已恢复",\n'
            '  "node_id": "{{ alert.node_id }}",\n'
            '  "timestamp": "{{ timestamp }}",\n'
            '  "dashboard_url": "{{ dashboard_url }}"\n'
            "}"
        ),
        "format": "text",
    },
    {
        "name": "事件创建 - Email",
        "event_type": "incident.created",
        "channel_type": "email",
        "subject_template": "[事件] {{ incident.title }}",
        "body_template": (
            "📌 **事件创建**\n\n"
            "- 标题：{{ incident.title }}\n"
            "- 状态：{{ incident.status }}\n"
            "- 时间：{{ timestamp }}\n\n"
            "{% if dashboard_url %}[查看详情]({{ dashboard_url }}){% endif %}"
        ),
        "format": "markdown",
    },
    {
        "name": "事件创建 - Webhook",
        "event_type": "incident.created",
        "channel_type": "webhook",
        "subject_template": None,
        "body_template": (
            "{\n"
            '  "event_type": "{{ event_type }}",\n'
            '  "incident_id": {{ incident.id }},\n'
            '  "title": "{{ incident.title }}",\n'
            '  "status": "{{ incident.status }}",\n'
            '  "timestamp": "{{ timestamp }}",\n'
            '  "dashboard_url": "{{ dashboard_url }}"\n'
            "}"
        ),
        "format": "text",
    },
    {
        "name": "事件解决 - Email",
        "event_type": "incident.resolved",
        "channel_type": "email",
        "subject_template": "[已解决] {{ incident.title }}",
        "body_template": (
            "✅ **事件已解决**\n\n"
            "- 标题：{{ incident.title }}\n"
            "- 事件 ID：{{ incident.id }}\n"
            "- 解决时间：{{ timestamp }}\n\n"
            "{% if dashboard_url %}[查看详情]({{ dashboard_url }}){% endif %}"
        ),
        "format": "markdown",
    },
    {
        "name": "事件解决 - Webhook",
        "event_type": "incident.resolved",
        "channel_type": "webhook",
        "subject_template": None,
        "body_template": (
            "{\n"
            '  "event_type": "{{ event_type }}",\n'
            '  "incident_id": {{ incident.id }},\n'
            '  "title": "{{ incident.title }}",\n'
            '  "status": "resolved",\n'
            '  "timestamp": "{{ timestamp }}",\n'
            '  "dashboard_url": "{{ dashboard_url }}"\n'
            "}"
        ),
        "format": "text",
    },
]

# Email/Webhook 基础模板（英文）
_BASE_TEMPLATES_EN: list[dict[str, Any]] = [
    {
        "name": "Alert Firing - Email",
        "event_type": "alert.firing",
        "channel_type": "email",
        "subject_template": "[{{ alert.severity | upper }}] Alert Firing: {{ alert.rule_name }}",
        "body_template": (
            "🚨 **{{ alert.severity | upper }}** Alert Firing\n\n"
            "- Rule: {{ alert.rule_name }}\n"
            "- Node: {{ alert.node_name }} ({{ alert.node_id }})\n"
            "- Metric: {{ alert.metric }} = {{ alert.value }}\n"
            "- Time: {{ timestamp }}\n\n"
            "[View Details]({{ dashboard_url }})"
        ),
        "format": "markdown",
    },
    {
        "name": "Alert Firing - Webhook",
        "event_type": "alert.firing",
        "channel_type": "webhook",
        "subject_template": None,
        "body_template": (
            "{\n"
            '  "event_type": "{{ event_type }}",\n'
            '  "severity": "{{ alert.severity }}",\n'
            '  "title": "{{ alert.rule_name }}",\n'
            '  "message": "Node {{ alert.node_name }} triggered an alert",\n'
            '  "metric": "{{ alert.metric }}",\n'
            '  "value": "{{ alert.value }}",\n'
            '  "node_id": "{{ alert.node_id }}",\n'
            '  "timestamp": "{{ timestamp }}",\n'
            '  "dashboard_url": "{{ dashboard_url }}"\n'
            "}"
        ),
        "format": "text",
    },
    {
        "name": "Alert Resolved - Email",
        "event_type": "alert.resolved",
        "channel_type": "email",
        "subject_template": "[Resolved] Alert Resolved: {{ alert.rule_name }}",
        "body_template": (
            "✅ **Alert Resolved**\n\n"
            "- Rule: {{ alert.rule_name }}\n"
            "- Node: {{ alert.node_name }} ({{ alert.node_id }})\n"
            "- Time: {{ timestamp }}\n\n"
            "[View Details]({{ dashboard_url }})"
        ),
        "format": "markdown",
    },
    {
        "name": "Alert Resolved - Webhook",
        "event_type": "alert.resolved",
        "channel_type": "webhook",
        "subject_template": None,
        "body_template": (
            "{\n"
            '  "event_type": "{{ event_type }}",\n'
            '  "severity": "{{ alert.severity }}",\n'
            '  "title": "Alert Resolved: {{ alert.rule_name }}",\n'
            '  "message": "Alert on node {{ alert.node_name }} has recovered",\n'
            '  "node_id": "{{ alert.node_id }}",\n'
            '  "timestamp": "{{ timestamp }}",\n'
            '  "dashboard_url": "{{ dashboard_url }}"\n'
            "}"
        ),
        "format": "text",
    },
    {
        "name": "Incident Created - Email",
        "event_type": "incident.created",
        "channel_type": "email",
        "subject_template": "[Incident] {{ incident.title }}",
        "body_template": (
            "📌 **Incident Created**\n\n"
            "- Title: {{ incident.title }}\n"
            "- Status: {{ incident.status }}\n"
            "- Time: {{ timestamp }}\n\n"
            "[View Details]({{ dashboard_url }})"
        ),
        "format": "markdown",
    },
    {
        "name": "Incident Created - Webhook",
        "event_type": "incident.created",
        "channel_type": "webhook",
        "subject_template": None,
        "body_template": (
            "{\n"
            '  "event_type": "{{ event_type }}",\n'
            '  "incident_id": {{ incident.id }},\n'
            '  "title": "{{ incident.title }}",\n'
            '  "status": "{{ incident.status }}",\n'
            '  "timestamp": "{{ timestamp }}",\n'
            '  "dashboard_url": "{{ dashboard_url }}"\n'
            "}"
        ),
        "format": "text",
    },
    {
        "name": "Incident Resolved - Email",
        "event_type": "incident.resolved",
        "channel_type": "email",
        "subject_template": "[Resolved] {{ incident.title }}",
        "body_template": (
            "✅ **Incident Resolved**\n\n"
            "- Title: {{ incident.title }}\n"
            "- Incident ID: {{ incident.id }}\n"
            "- Resolved At: {{ timestamp }}\n\n"
            "[View Details]({{ dashboard_url }})"
        ),
        "format": "markdown",
    },
    {
        "name": "Incident Resolved - Webhook",
        "event_type": "incident.resolved",
        "channel_type": "webhook",
        "subject_template": None,
        "body_template": (
            "{\n"
            '  "event_type": "{{ event_type }}",\n'
            '  "incident_id": {{ incident.id }},\n'
            '  "title": "{{ incident.title }}",\n'
            '  "status": "resolved",\n'
            '  "timestamp": "{{ timestamp }}",\n'
            '  "dashboard_url": "{{ dashboard_url }}"\n'
            "}"
        ),
        "format": "text",
    },
]

# 保留历史导出名（默认中文套，测试与外部引用兼容）
DEFAULT_TEMPLATES: list[dict[str, Any]] = _BASE_TEMPLATES_ZH + _im_templates("zh")
DEFAULT_TEMPLATES_EN: list[dict[str, Any]] = _BASE_TEMPLATES_EN + _im_templates("en")


def get_default_templates(locale: str) -> list[dict[str, Any]]:
    """按语言返回默认模板定义；zh 开头走中文套，其余走英文套。"""
    if (locale or "zh").lower().startswith("zh"):
        return DEFAULT_TEMPLATES
    return DEFAULT_TEMPLATES_EN


def init_default_templates(session: Session, locale: str | None = None) -> None:
    """将系统默认模板初始化到数据库（幂等，insert-only 不覆盖存量）。

    locale 缺省取 ``settings.DEFAULT_LOCALE``（默认 zh）。
    """
    effective_locale = locale or settings.DEFAULT_LOCALE
    for template_data in get_default_templates(effective_locale):
        existing = session.exec(
            select(NotificationTemplate).where(
                NotificationTemplate.event_type == template_data["event_type"],
                NotificationTemplate.channel_type == template_data["channel_type"],
                NotificationTemplate.is_default == True,  # noqa: E712
            )
        ).first()
        if existing is not None:
            continue

        template = NotificationTemplate(
            name=template_data["name"],
            event_type=template_data["event_type"],
            channel_type=template_data["channel_type"],
            subject_template=template_data["subject_template"],
            body_template=template_data["body_template"],
            format=template_data["format"],
            is_default=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(template)
        logger.info(
            f"初始化默认模板: {template.event_type}/{template.channel_type}"
        )

    session.commit()


def get_dashboard_url(path: str = "") -> str:
    """根据 PUBLIC_SERVER_URL 构造仪表盘链接。"""
    base = settings.PUBLIC_SERVER_URL or ""
    if not base:
        return ""
    if path:
        return f"{base.rstrip('/')}/{path.lstrip('/')}"
    return base.rstrip("/")
