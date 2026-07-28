"""Watchdog 配置。

支持从环境变量读取，未配置时使用默认值。
默认启用，生产环境由 systemd 服务拉起；开发环境可通过环境变量关闭。
"""

import os
from dataclasses import dataclass


@dataclass
class WatchdogConfig:
    """独立 Watchdog 进程配置。"""

    enabled: bool = True
    check_interval_seconds: int = 5
    failure_threshold: int = 3
    health_url: str = "http://localhost:8000/health"
    health_timeout_seconds: float = 3.0
    restart_command: str = "systemctl restart chaosops-server"
    max_backoff_seconds: int = 300

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        """从环境变量加载配置，未设置时保留默认值。"""
        return cls(
            enabled=_parse_bool(
                os.environ.get("CHAOSOPS_WATCHDOG_ENABLED", str(cls.enabled))
            ),
            check_interval_seconds=int(
                os.environ.get(
                    "CHAOSOPS_WATCHDOG_CHECK_INTERVAL_SECONDS",
                    str(cls.check_interval_seconds),
                )
            ),
            failure_threshold=int(
                os.environ.get(
                    "CHAOSOPS_WATCHDOG_FAILURE_THRESHOLD",
                    str(cls.failure_threshold),
                )
            ),
            health_url=os.environ.get(
                "CHAOSOPS_WATCHDOG_HEALTH_URL",
                cls.health_url,
            ),
            health_timeout_seconds=float(
                os.environ.get(
                    "CHAOSOPS_WATCHDOG_HEALTH_TIMEOUT_SECONDS",
                    str(cls.health_timeout_seconds),
                )
            ),
            restart_command=os.environ.get(
                "CHAOSOPS_WATCHDOG_RESTART_COMMAND",
                cls.restart_command,
            ),
            max_backoff_seconds=int(
                os.environ.get(
                    "CHAOSOPS_WATCHDOG_MAX_BACKOFF_SECONDS",
                    str(cls.max_backoff_seconds),
                )
            ),
        )


def _parse_bool(value: str) -> bool:
    """解析布尔值环境变量。"""
    return value.lower() in ("true", "1", "yes", "on")
