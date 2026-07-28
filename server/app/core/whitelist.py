"""远程执行命令白名单。

用于校验用户通过 Web 控制台手动触发的远程命令是否在允许列表内。
禁止执行任意 Shell 命令。
"""

from app.core.logger import get_logger

logger = get_logger("core.whitelist")

# 远程执行允许的白名单动作。
# 仅包含预置诊断/运维动作，禁止任意 Shell 执行。
REMOTE_ACTION_ALLOWLIST = {
    "restart_service",
    "restart_container",
    "clear_log",
    "kill_process",
    "run_script",
    "view_logs",
    "disk_usage",
    "service_status",
}


def validate_remote_action(action_id: str) -> bool:
    """检查 action_id 是否在远程执行白名单内。"""
    if not action_id:
        return False
    allowed = action_id in REMOTE_ACTION_ALLOWLIST
    if not allowed:
        logger.warning(f"远程执行拒绝非白名单动作: {action_id}")
    return allowed


def list_remote_actions() -> list[dict]:
    """返回白名单动作列表，供前端选择。"""
    # name 存 i18n key，前端按 heal.builtin.<action_id>.name 翻译，
    # 取不到 key 时 fallback 原样显示。
    action_metadata = {
        "restart_service": {"name": "heal.builtin.restart_service.name", "risk_level": "medium"},
        "restart_container": {"name": "heal.builtin.restart_container.name", "risk_level": "medium"},
        "clear_log": {"name": "heal.builtin.clear_log.name", "risk_level": "low"},
        "kill_process": {"name": "heal.builtin.kill_process.name", "risk_level": "high"},
        "run_script": {"name": "heal.builtin.run_script.name", "risk_level": "high"},
        "view_logs": {"name": "heal.builtin.view_logs.name", "risk_level": "low"},
        "disk_usage": {"name": "heal.builtin.disk_usage.name", "risk_level": "low"},
        "service_status": {"name": "heal.builtin.service_status.name", "risk_level": "low"},
    }
    return [
        {"action_id": action_id, **action_metadata[action_id]}
        for action_id in sorted(REMOTE_ACTION_ALLOWLIST)
    ]
