"""API 错误消息字典与 Accept-Language 选择。

设计：
- 固定文案的 HTTPException detail 改为错误码，按请求 Accept-Language 返回 zh/en；
  缺省（无请求或无该头）返回 zh —— 测试客户端不带 Accept-Language，中文断言不破。
- 服务层 ValueError 经 API ``detail=str(e)`` 透传的，用 ``SERVICE_ERROR_CODES``
  按中文原文精确匹配映射到错误码再翻译；动态插值消息匹配不到时原样返回（zh）。

未覆盖的错误消息（低频/动态）保持中文原样，后续批次增量补充。
"""

from typing import Optional

from fastapi import Request

# code -> {zh, en}。zh 必须与历史文案逐字一致（测试断言 + 前端已适配）。
_MESSAGES: dict[str, dict[str, str]] = {
    # ---- 认证 / 授权（deps.py / auth.py） ----
    "auth.invalid_credentials": {"zh": "无法验证凭据", "en": "Invalid credentials"},
    "auth.expired": {"zh": "凭据已过期", "en": "Credentials expired"},
    "auth.invalidated": {"zh": "凭据已失效", "en": "Credentials invalidated"},
    "auth.admin_required": {"zh": "需要 admin 权限", "en": "Admin role required"},
    "auth.bad_credentials": {
        "zh": "用户名或密码错误",
        "en": "Incorrect username or password",
    },
    "auth.account_disabled": {"zh": "账号已被禁用", "en": "Account disabled"},
    "auth.reset_token_invalid": {
        "zh": "重置链接无效或已过期",
        "en": "Reset link invalid or expired",
    },
    "license.feature_locked": {
        "zh": "当前 License 未解锁功能: {feature}",
        "en": "Feature not unlocked by current license: {feature}",
    },
    # ---- License 自助生成（license_claim.py） ----
    # 统一错误（规格 §4.1）：绝不区分订单不存在/邮箱不匹配/install_id 问题
    "license_claim.mismatch": {
        "zh": "订单号、邮箱或 install_id 不匹配",
        "en": "Order ID, email, or install_id does not match",
    },
    "license_claim.rate_limited": {
        "zh": "请求过于频繁，请稍后再试",
        "en": "Too many requests, please try again later",
    },
    "license_claim.already_claimed": {
        "zh": "该订单已生成过 License，请联系客服 contact@chaosm.io",
        "en": "A license has already been generated for this order. Please contact contact@chaosm.io",
    },
    "license_claim.verify_unavailable": {
        "zh": "订单核验暂不可用，请稍后再试",
        "en": "Order verification is temporarily unavailable, please try again later",
    },
    "license_claim.issue_failed": {
        "zh": "License 签发失败，请联系客服 contact@chaosm.io",
        "en": "License issuance failed, please contact contact@chaosm.io",
    },
    "license_claim.download_invalid": {
        "zh": "下载链接无效或已过期",
        "en": "Download link is invalid or expired",
    },
    "license_claim.success": {
        "zh": "License 已生成并发送至您的邮箱",
        "en": "License generated and sent to your email",
    },
    # ---- 用户管理（users.py） ----
    "users.username_exists": {"zh": "用户名已存在", "en": "Username already exists"},
    "users.email_exists": {"zh": "邮箱已存在", "en": "Email already exists"},
    "users.not_found": {"zh": "用户不存在", "en": "User not found"},
    "users.last_admin_demote": {
        "zh": "不能将最后一个 admin 用户降级为 viewer",
        "en": "Cannot demote the last admin to viewer",
    },
    "users.last_admin_disable": {
        "zh": "不能禁用最后一个 admin 用户",
        "en": "Cannot disable the last admin",
    },
    "users.last_admin_delete": {
        "zh": "不能删除最后一个 admin 用户",
        "en": "Cannot delete the last admin",
    },
    "users.delete_self": {
        "zh": "不能删除当前登录用户",
        "en": "Cannot delete the current logged-in user",
    },
    # ---- 事件（incidents.py + services/incident.py） ----
    "incidents.not_found": {"zh": "事件不存在", "en": "Incident not found"},
    "incidents.merge_source_not_found": {
        "zh": "被合并事件不存在",
        "en": "Source incident not found",
    },
    "incidents.merge_target_not_found": {
        "zh": "目标事件不存在",
        "en": "Target incident not found",
    },
    "incidents.closed_no_ack": {
        "zh": "事件已关闭，无法认领",
        "en": "Incident closed, cannot acknowledge",
    },
    "incidents.ack_only_open": {
        "zh": "只有 open 状态的事件可以认领",
        "en": "Only open incidents can be acknowledged",
    },
    "incidents.closed_no_resolve": {
        "zh": "事件已关闭，无法标记解决",
        "en": "Incident closed, cannot mark resolved",
    },
    "incidents.resolve_only_open_ack": {
        "zh": "只有 open 或 acknowledged 状态的事件可以标记解决",
        "en": "Only open or acknowledged incidents can be marked resolved",
    },
    "incidents.already_closed": {"zh": "事件已关闭", "en": "Incident already closed"},
    "incidents.close_only_resolved": {
        "zh": "只有 resolved 状态的事件可以关闭",
        "en": "Only resolved incidents can be closed",
    },
    "incidents.merge_self": {"zh": "不能合并到自身", "en": "Cannot merge into itself"},
    "incidents.merge_source_closed": {
        "zh": "被合并事件已关闭",
        "en": "Source incident already closed",
    },
    "incidents.merge_target_closed": {
        "zh": "目标事件已关闭",
        "en": "Target incident already closed",
    },
    "incidents.merge_deleted": {
        "zh": "不能合并已删除的事件",
        "en": "Cannot merge deleted incidents",
    },
    "incidents.closed_no_update": {
        "zh": "事件已关闭，无法更新",
        "en": "Incident closed, cannot update",
    },
    "incidents.deleted": {"zh": "事件已删除", "en": "Incident deleted"},
    # ---- 节点（nodes.py + services/node_service.py） ----
    "nodes.not_found": {"zh": "节点不存在", "en": "Node not found"},
    "nodes.csv_required": {"zh": "请上传 CSV 文件", "en": "Please upload a CSV file"},
    "nodes.csv_encoding": {
        "zh": "CSV 文件编码错误，请使用 UTF-8 编码",
        "en": "CSV file encoding error, please use UTF-8",
    },
    "nodes.bulk_task_not_found": {
        "zh": "批量任务不存在",
        "en": "Bulk task not found",
    },
    "nodes.public_url_missing_script": {
        "zh": "未配置 PUBLIC_SERVER_URL，无法生成安装脚本",
        "en": "PUBLIC_SERVER_URL not configured, cannot generate install script",
    },
    "nodes.public_url_missing_cmd": {
        "zh": "未配置 PUBLIC_SERVER_URL，无法生成安装命令",
        "en": "PUBLIC_SERVER_URL not configured, cannot generate install command",
    },
    "nodes.local_token_revoke_forbidden": {
        "zh": "本地默认节点不允许撤销 Token",
        "en": "Revoking token of the local default node is not allowed",
    },
    "nodes.local_delete_forbidden": {
        "zh": "本地默认节点不允许删除",
        "en": "Deleting the local default node is not allowed",
    },
    "nodes.local_token_reset_forbidden": {
        "zh": "本地默认节点不允许重置 Token",
        "en": "Resetting token of the local default node is not allowed",
    },
    "nodes.name_exists": {"zh": "节点名称已存在", "en": "Node name already exists"},
    # ---- Agent 版本（agent_version.py） ----
    "agent_version.invalid_format": {
        "zh": "版本号格式必须为 x.y.z",
        "en": "Version must be in x.y.z format",
    },
    "agent_version.invalid_channel": {
        "zh": "channel 必须是 stable 或 beta",
        "en": "Channel must be stable or beta",
    },
    "agent_version.invalid_checksum": {
        "zh": "checksum 必须是 64 位 SHA256 十六进制字符串",
        "en": "Checksum must be a 64-char SHA256 hex string",
    },
    "agent_version.exists": {
        "zh": "版本 {version} 已存在",
        "en": "Version {version} already exists",
    },
    "agent_version.invalid_filename": {"zh": "非法文件名", "en": "Invalid filename"},
    "agent_version.dist_not_found": {
        "zh": "更新包不存在",
        "en": "Distribution package not found",
    },
    "agent_version.task_not_found": {
        "zh": "更新任务不存在",
        "en": "Update task not found",
    },
    "agent_version.report_forbidden": {
        "zh": "无权上报其他节点的更新任务",
        "en": "Not allowed to report update tasks of other nodes",
    },
}

# 服务层 ValueError 中文原文 -> 错误码（仅静态文案可精确匹配；
# 动态插值消息如 "主机地址 'x' 已存在" 匹配不到，原样返回）。
SERVICE_ERROR_CODES: dict[str, str] = {
    # services/incident.py
    "事件已关闭，无法认领": "incidents.closed_no_ack",
    "只有 open 状态的事件可以认领": "incidents.ack_only_open",
    "事件已关闭，无法标记解决": "incidents.closed_no_resolve",
    "只有 open 或 acknowledged 状态的事件可以标记解决": "incidents.resolve_only_open_ack",
    "事件已关闭": "incidents.already_closed",
    "只有 resolved 状态的事件可以关闭": "incidents.close_only_resolved",
    "不能合并到自身": "incidents.merge_self",
    "被合并事件已关闭": "incidents.merge_source_closed",
    "目标事件已关闭": "incidents.merge_target_closed",
    "不能合并已删除的事件": "incidents.merge_deleted",
    "事件已关闭，无法更新": "incidents.closed_no_update",
    "事件已删除": "incidents.deleted",
    # services/node_service.py
    "节点名称已存在": "nodes.name_exists",
    "本地默认节点不允许删除": "nodes.local_delete_forbidden",
    "本地默认节点不允许重置 Token": "nodes.local_token_reset_forbidden",
    "本地默认节点不允许撤销 Token": "nodes.local_token_revoke_forbidden",
}


def locale_from_request(request: Optional[Request]) -> str:
    """从 Accept-Language 解析语言，缺省 zh。"""
    if request is None:
        return "zh"
    header = request.headers.get("accept-language", "")
    return "en" if header.lower().startswith("en") else "zh"


def get_error_message(code: str, locale: str = "zh", **params) -> str:
    """按 code+locale 取错误文案；code 未知时原样返回 code（不崩）。"""
    entry = _MESSAGES.get(code)
    if entry is None:
        return code
    text = entry.get(locale) or entry["zh"]
    return text.format(**params) if params else text


def error_detail(request: Optional[Request], code: str, **params) -> str:
    """HTTPException detail 取值入口。"""
    return get_error_message(code, locale_from_request(request), **params)


def error_detail_from_exception(request: Optional[Request], exc: Exception) -> str:
    """服务层 ValueError 透传翻译：静态文案映射到 code，匹配不到原样返回。"""
    message = str(exc)
    code = SERVICE_ERROR_CODES.get(message)
    if code is None:
        return message
    return get_error_message(code, locale_from_request(request))
