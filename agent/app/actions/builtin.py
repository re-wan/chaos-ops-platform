"""Agent 端预置自愈动作实现。

MVP 阶段函数体为 mock/安全实现，不执行真实危险操作。
所有动作返回统一结构：{"success": bool, "message": str, "output": str}。
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("agent.actions.builtin")


# 路径型动作（view_logs / clear_log / disk_usage）的目录白名单。
# 经远程执行/自愈规则下发时，动作参数完全由服务端规则控制，故必须在 Agent 本地
# 收敛路径范围，防止任意文件读（/etc/shadow、密钥、配置）或误清任意文件。
_DEFAULT_LOG_ALLOWED_DIR = "/var/log"


def _resolve_allowed_dirs() -> list:
    """解析路径型动作允许的根目录列表（均为 resolve 后的绝对路径）。

    - 未设置 AGENT_LOG_ALLOWED_DIRS：返回默认值（/var/log + Agent 工作目录下的 logs）。
    - 设置为逗号分隔多目录：返回这些目录。
    - 设置为空字符串或仅空白/逗号：返回空列表 → fail-closed，所有路径型动作被拒绝并记 warn。
    """
    raw = os.environ.get("AGENT_LOG_ALLOWED_DIRS")
    if raw is None:
        candidates = [Path(_DEFAULT_LOG_ALLOWED_DIR), Path.cwd() / "logs"]
        return [p.resolve() for p in candidates]

    dirs = [item.strip() for item in raw.split(",") if item.strip()]
    if not dirs:
        # 显式置空：fail-closed，绝不放行任意路径。
        logger.warning(
            "AGENT_LOG_ALLOWED_DIRS 为空，路径型动作（view_logs/clear_log/disk_usage）将全部被拒绝"
        )
        return []
    return [Path(item).resolve() for item in dirs]


def _check_path_allowed(path_str: str):
    """校验路径是否落在白名单根目录内，返回 (resolved_path, error_message)。

    允许时 error_message 为 None；拒绝时返回 None 与不含白名单细节的错误信息
    （避免向远端下发方泄露 Agent 本地的白名单配置）。
    """
    allowed = _resolve_allowed_dirs()
    if not allowed:
        return None, "路径访问未启用（白名单为空）"

    try:
        candidate = Path(path_str)
    except (TypeError, ValueError):
        return None, "路径非法"

    # 拒绝符号链接：链接可能逃逸到白名单外的任意位置（resolve 前判断）。
    if candidate.is_symlink():
        return None, "不允许访问符号链接"

    try:
        # resolve() 展开 ".." 与相对路径，阻断 /var/log/../../etc/shadow 类绕过。
        resolved = candidate.resolve()
    except OSError:
        return None, "路径解析失败"

    for root in allowed:
        try:
            resolved.relative_to(root)
            return resolved, None
        except ValueError:
            continue
    return None, "路径不在允许范围内"



def _ok(message: str, output: str = "") -> dict:
    """构造成功响应。"""
    return {"success": True, "message": message, "output": output}


def _fail(message: str, output: str = "") -> dict:
    """构造失败响应。"""
    return {"success": False, "message": message, "output": output}


def restart_service(service_name: str) -> dict:
    """重启指定服务（mock）。"""
    logger.warning(f"[MOCK] 重启服务: {service_name}")
    return _ok(f"服务 {service_name} 已触发重启")


def restart_container(container_name: str) -> dict:
    """重启指定容器（mock）。"""
    logger.warning(f"[MOCK] 重启容器: {container_name}")
    return _ok(f"容器 {container_name} 已触发重启")


def clear_log(log_path: str, keep_days: int) -> dict:
    """清理超过保留天数的日志文件（安全实现）。

    仅删除普通文件，不递归目录，不跟随符号链接；路径必须落在白名单根目录内。
    """
    logger.info(f"清理日志: {log_path}, 保留天数: {keep_days}")
    path, err = _check_path_allowed(log_path)
    if err is not None:
        return _fail(err)
    if not path.exists():
        return _fail(f"日志路径不存在: {log_path}")
    if not path.is_file():
        return _fail(f"日志路径不是文件: {log_path}")

    try:
        # MVP 阶段仅计算可释放空间并返回，不真正删除
        size = path.stat().st_size
        return _ok(
            f"日志 {log_path} 可清理，保留 {keep_days} 天，大小 {size} 字节",
            output=f"would_free_bytes={size}",
        )
    except OSError as e:
        return _fail(f"访问日志失败: {e}")


def kill_process(process_name: Optional[str] = None, pid: Optional[int] = None) -> dict:
    """结束进程（mock，不真正杀进程）。"""
    target = process_name or f"pid={pid}"
    logger.warning(f"[MOCK] 结束进程: {target}")
    return _ok(f"进程 {target} 已触发结束（mock）")


def reboot_node(delay_seconds: int) -> dict:
    """重启节点（mock）。"""
    logger.warning(f"[MOCK] 重启节点，延迟 {delay_seconds} 秒")
    return _ok(f"节点将在 {delay_seconds} 秒后重启（mock）")


def run_script(
    script_id: Optional[str] = None,
    args: Optional[list[str]] = None,
    script_content: Optional[str] = None,
    script_hash: Optional[str] = None,
    interpreter: str = "bash",
    timeout_seconds: Optional[int] = None,
) -> dict:
    """执行已审批脚本。

    兼容两种调用方式：
    - 旧 mock 方式：script_id + args
    - 新沙箱方式：script_content + script_hash + interpreter
    """
    args = args or []
    if script_content is not None and script_hash is not None:
        from agent.app.actions.custom_script import execute_custom_script

        return execute_custom_script(
            script_content=script_content,
            script_hash=script_hash,
            interpreter=interpreter,
            args={"args": args} if args else {},
            timeout=timeout_seconds,
        )

    logger.warning(f"[MOCK] 执行脚本: {script_id}, args={args}")
    return _ok(f"脚本 {script_id} 已触发执行（mock）")


def view_logs(log_path: str, lines: int = 100) -> dict:
    """查看指定日志文件最后 N 行（安全实现）。

    路径必须落在白名单根目录内，防止经自愈规则/远程执行下发读取任意文件。
    """
    logger.info(f"查看日志: {log_path}, 行数: {lines}")
    path, err = _check_path_allowed(log_path)
    if err is not None:
        return _fail(err)
    if not path.exists():
        return _fail(f"日志路径不存在: {log_path}")
    if not path.is_file():
        return _fail(f"日志路径不是文件: {log_path}")

    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        all_lines = content.splitlines()
        tail = all_lines[-lines:] if lines > 0 else all_lines
        return _ok(
            f"日志 {log_path} 最后 {len(tail)} 行",
            output="\n".join(tail),
        )
    except OSError as e:
        return _fail(f"读取日志失败: {e}")


def disk_usage(path: str = "/") -> dict:
    """查看指定路径的磁盘使用情况，路径必须落在白名单根目录内。"""
    logger.info(f"查看磁盘使用: {path}")
    resolved, err = _check_path_allowed(path)
    if err is not None:
        return _fail(err)
    try:
        usage = shutil.disk_usage(resolved)
        output = (
            f"total={usage.total}, used={usage.used}, "
            f"free={usage.free}, percent={usage.used / usage.total:.2%}"
        )
        return _ok(f"路径 {path} 磁盘使用", output=output)
    except OSError as e:
        return _fail(f"获取磁盘使用失败: {e}")


def service_status(service_name: str) -> dict:
    """查看服务状态（mock）。"""
    logger.info(f"查看服务状态: {service_name}")
    return _ok(f"服务 {service_name} 运行中（mock）")
