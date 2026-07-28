"""Agent 自愈动作包。

提供可在 Agent 节点本地执行的预置自愈动作。
"""

from agent.app.actions.builtin import (
    clear_log,
    kill_process,
    reboot_node,
    restart_container,
    restart_service,
    run_script,
)

__all__ = [
    "clear_log",
    "kill_process",
    "reboot_node",
    "restart_container",
    "restart_service",
    "run_script",
]
