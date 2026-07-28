"""系统邮件发送服务。

用于密码重置等系统级邮件，配置独立于通知渠道的 EmailBackend。
"""

import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("core.mailer")


def send_password_reset_email(to_addr: str, reset_url: str) -> dict:
    """发送密码重置邮件。

    Args:
        to_addr: 收件人邮箱地址。
        reset_url: 密码重置链接。

    Returns:
        {"success": bool, "error_message": Optional[str]}
    """
    smtp_host = settings.SMTP_HOST
    smtp_port = settings.SMTP_PORT
    username = settings.SMTP_USERNAME
    password = settings.SMTP_PASSWORD
    use_tls = settings.SMTP_USE_TLS
    from_addr = settings.SMTP_FROM_ADDR or username

    if not smtp_host or not username:
        logger.error("SMTP 未配置，无法发送密码重置邮件")
        return {
            "success": False,
            "error_message": "SMTP 未配置",
        }

    subject = "[ChaosOps] 密码重置请求"
    body = (
        "您好，\n\n"
        "您刚才请求重置 ChaosOps 账号密码。请点击以下链接完成重置：\n\n"
        f"{reset_url}\n\n"
        "该链接 15 分钟内有效，且只能使用一次。\n\n"
        "如果您没有请求重置密码，请忽略此邮件。\n\n"
        "ChaosOps"
    )

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = from_addr or username
        msg["To"] = to_addr

        with smtplib.SMTP(smtp_host, int(smtp_port), timeout=30) as server:
            if use_tls:
                server.starttls()
            if password:
                server.login(username, password)
            server.sendmail(from_addr or username, [to_addr], msg.as_string())

        logger.info(f"密码重置邮件发送成功: to={to_addr}")
        return {"success": True, "error_message": None}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"密码重置邮件发送失败: to={to_addr}, error={exc}")
        return {"success": False, "error_message": str(exc)}


def build_password_reset_url(token: str) -> str:
    """构造密码重置链接。"""
    base_url = settings.PUBLIC_SERVER_URL or ""
    # 去除末尾斜杠，统一拼接 /auth/reset-password
    base_url = base_url.rstrip("/")
    return f"{base_url}/auth/reset-password?token={token}"


def send_license_email(to_addr: str, license_id: str, license_json: str) -> dict:
    """发送 License 交付邮件（含 License 文件附件）。

    Args:
        to_addr: 客户购买邮箱。
        license_id: 已签发的 License ID。
        license_json: 签名后的 License 文件内容（JSON 文本，作为附件）。

    Returns:
        {"success": bool, "error_message": Optional[str]}。
        SMTP 未配置时返回失败（由调用方决定不阻断页面下载）。
    """
    smtp_host = settings.SMTP_HOST
    smtp_port = settings.SMTP_PORT
    username = settings.SMTP_USERNAME
    password = settings.SMTP_PASSWORD
    use_tls = settings.SMTP_USE_TLS
    from_addr = settings.SMTP_FROM_ADDR or username

    if not smtp_host or not username:
        logger.error("SMTP 未配置，无法发送 License 交付邮件")
        return {"success": False, "error_message": "SMTP 未配置"}

    subject = "[ChaosOps] 您的 License 已生成"
    body = (
        "您好，\n\n"
        "您的 ChaosOps License 已生成，License 文件见附件。\n\n"
        f"License ID: {license_id}\n\n"
        "请在 ChaosOps 控制台「设置 → License」中上传该文件完成激活。\n"
        "如有任何问题，请联系 contact@chaosm.io。\n\n"
        "ChaosOps"
    )

    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = from_addr or username
        msg["To"] = to_addr
        msg.attach(MIMEText(body, "plain", "utf-8"))
        attachment = MIMEApplication(license_json.encode("utf-8"), "json")
        attachment.add_header(
            "Content-Disposition",
            "attachment",
            filename=f"chaosops-license-{license_id}.json",
        )
        msg.attach(attachment)

        with smtplib.SMTP(smtp_host, int(smtp_port), timeout=30) as server:
            if use_tls:
                server.starttls()
            if password:
                server.login(username, password)
            server.sendmail(from_addr or username, [to_addr], msg.as_string())

        logger.info(f"License 交付邮件发送成功: to={to_addr}, license_id={license_id}")
        return {"success": True, "error_message": None}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"License 交付邮件发送失败: to={to_addr}, error={exc}")
        return {"success": False, "error_message": str(exc)}
