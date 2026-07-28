"""通知模板渲染服务。

基于 Jinja2，支持自定义模板覆盖默认模板，变量缺失时不报错。

安全：自定义模板由管理员编写，但仍按深度防御处理——使用 Jinja2 沙箱环境，
禁止模板访问 Python 内部属性（``__class__`` / ``__mro__`` / ``__globals__`` 等），
防止服务端模板注入（SSTI）升级为远程代码执行。命中沙箱限制时抛
``jinja2.exceptions.SecurityError``，由渲染回退逻辑兜底。
"""

from datetime import datetime, timezone
from typing import Any, Optional

import jinja2
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment
from sqlmodel import Session, select

from app.core.default_templates import get_dashboard_url, get_default_templates
from app.core.logger import get_logger
from app.models.notification_template import NotificationTemplate

logger = get_logger("services.template_renderer")


class _StrictSandboxedEnvironment(SandboxedEnvironment):
    """在沙箱之上收紧：不安全属性访问直接抛 SecurityError。

    默认 ``SandboxedEnvironment.unsafe_undefined`` 在配合 DebugUndefined 时只输出一段
    调试文本而不抛错，恶意模板会静默渲染出 "unsafe" 提示而非失败；改为硬失败后，
    恶意模板走正常渲染回退（自定义模板 → 默认模板 → 简单消息）。
    """

    def unsafe_undefined(self, obj: Any, attribute: str) -> Any:
        raise SecurityError(
            f"access to attribute {attribute!r} of "
            f"{type(obj).__name__!r} object is unsafe."
        )


# 使用 DebugUndefined，变量缺失时渲染为空字符串且不抛错；
# 沙箱拦截对下划线/内部属性的访问，dict 属性访问与常规过滤器不受影响。
_jinja_env = _StrictSandboxedEnvironment(undefined=jinja2.DebugUndefined)


_MAX_RENDERED_BYTES = 256 * 1024


def _render_template(template_str: str, context: dict[str, Any]) -> str:
    """使用 Jinja2 渲染模板字符串。"""
    template = _jinja_env.from_string(template_str)
    return template.render(context)


def _truncate_rendered(text: str) -> str:
    """限制渲染输出大小，防止模板滥用。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= _MAX_RENDERED_BYTES:
        return text
    truncated = encoded[:_MAX_RENDERED_BYTES]
    # 避免截断在 UTF-8 多字节字符中间
    return truncated.decode("utf-8", errors="ignore")


def _get_template(
    session: Session, event_type: str, channel_type: str
) -> Optional[NotificationTemplate]:
    """获取模板：优先自定义模板，否则默认模板。"""
    # 先查自定义模板
    custom = session.exec(
        select(NotificationTemplate).where(
            NotificationTemplate.event_type == event_type,
            NotificationTemplate.channel_type == channel_type,
            NotificationTemplate.is_default == False,  # noqa: E712
        )
    ).first()
    if custom is not None:
        return custom

    # 再查默认模板
    default = session.exec(
        select(NotificationTemplate).where(
            NotificationTemplate.event_type == event_type,
            NotificationTemplate.channel_type == channel_type,
            NotificationTemplate.is_default == True,  # noqa: E712
        )
    ).first()
    return default


def _get_builtin_template(
    event_type: str, channel_type: str, lang: str
) -> Optional[NotificationTemplate]:
    """从代码内置的默认模板定义构造临时模板对象（不落库）。

    用于按渠道语言渲染：数据库只 seed 了一套默认模板（由 DEFAULT_LOCALE 决定），
    英文渠道需要英文文案时直接取代码内的英文定义，与部署 locale 解耦。
    """
    for template_data in get_default_templates(lang):
        if (
            template_data["event_type"] == event_type
            and template_data["channel_type"] == channel_type
        ):
            return NotificationTemplate(
                name=template_data["name"],
                event_type=event_type,
                channel_type=channel_type,
                subject_template=template_data["subject_template"],
                body_template=template_data["body_template"],
                format=template_data["format"],
                is_default=True,
            )
    return None


def _render_with_fallback(
    session: Session,
    template: NotificationTemplate,
    context: dict[str, Any],
    fallback_body: str,
) -> dict[str, Any]:
    """渲染模板，失败时回退到默认模板或简单消息。"""
    try:
        body = _truncate_rendered(_render_template(template.body_template, context))
        subject = None
        if template.subject_template is not None:
            subject = _truncate_rendered(
                _render_template(template.subject_template, context)
            )
        return {
            "subject": subject,
            "body": body,
            "format": template.format,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"模板渲染失败，尝试回退: event_type={template.event_type}, "
            f"channel_type={template.channel_type}, error={exc}"
        )

    # 如果是自定义模板失败，尝试同组合默认模板
    if not template.is_default:
        default_template = session.exec(
            select(NotificationTemplate).where(
                NotificationTemplate.event_type == template.event_type,
                NotificationTemplate.channel_type == template.channel_type,
                NotificationTemplate.is_default == True,  # noqa: E712
            )
        ).first()
        if default_template is not None:
                try:
                    body = _truncate_rendered(
                        _render_template(default_template.body_template, context)
                    )
                    subject = None
                    if default_template.subject_template is not None:
                        subject = _truncate_rendered(
                            _render_template(
                                default_template.subject_template, context
                            )
                        )
                    return {
                        "subject": subject,
                        "body": body,
                        "format": default_template.format,
                    }
                except Exception as fallback_exc:  # noqa: BLE001
                    logger.warning(
                        f"默认模板渲染也失败: event_type={template.event_type}, "
                        f"error={fallback_exc}"
                    )

    return {
        "subject": None,
        "body": fallback_body,
        "format": "text",
    }


def render_notification(
    session: Session,
    event_type: str,
    channel_type: str,
    context: dict[str, Any],
    lang: str = "zh",
) -> dict[str, Any]:
    """渲染通知模板。

    lang 为渠道通知语言（zh / en）：自定义模板优先（语言由管理员自负）；
    非中文渠道且无自定义模板时，使用代码内置的对应语言默认模板，
    与数据库 seed 的 locale 解耦。缺省 zh，行为与历史版本一致。

    Returns:
        {"subject": Optional[str], "body": str, "format": str}
    """
    template = _get_template(session, event_type, channel_type)

    if (
        lang
        and not lang.lower().startswith("zh")
        and (template is None or template.is_default)
    ):
        builtin = _get_builtin_template(event_type, channel_type, lang)
        if builtin is not None:
            template = builtin

    # 统一补充上下文
    full_context = {
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dashboard_url": get_dashboard_url(),
    }
    full_context.update(context)

    fallback_body = f"[{event_type}] 通知内容渲染失败或模板不存在"
    if template is None:
        logger.warning(
            f"未找到模板: event_type={event_type}, channel_type={channel_type}"
        )
        return {
            "subject": None,
            "body": fallback_body,
            "format": "text",
        }

    return _render_with_fallback(session, template, full_context, fallback_body)


def preview_template(
    session: Session,
    template: NotificationTemplate,
    context: dict[str, Any],
) -> dict[str, Any]:
    """预览指定模板的渲染结果。

    与 render_notification 不同，此函数直接使用传入模板，不查找覆盖模板。
    """
    full_context = {
        "event_type": template.event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dashboard_url": get_dashboard_url(),
    }
    full_context.update(context)

    try:
        body = _truncate_rendered(
            _render_template(template.body_template, full_context)
        )
        subject = None
        if template.subject_template is not None:
            subject = _truncate_rendered(
                _render_template(template.subject_template, full_context)
            )
        return {
            "subject": subject,
            "body": body,
            "format": template.format,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"预览模板渲染失败: template_id={template.id}, error={exc}")
        return {
            "subject": None,
            "body": f"模板渲染失败: {exc}",
            "format": "text",
        }
