#!/usr/bin/env python3
"""ChaosOps 独立 Watchdog 进程。

监控 Server 的 /health 接口，连续失败达到阈值时重启 Server。
作为独立进程运行，与 Server 解耦。
"""

import signal
import subprocess
import sys
import time
import urllib.request
from types import FrameType
from typing import Callable, Optional

from app.core.logger import get_logger, setup_logging
from app.core.watchdog_config import WatchdogConfig


class WatchdogRunner:
    """Watchdog 监控循环实现。"""

    def __init__(
        self,
        config: WatchdogConfig,
        restart_func: Optional[Callable[[], None]] = None,
        http_client: Optional[Callable[[str, float], object]] = None,
    ):
        self.config = config
        self._restart_func = restart_func or self._default_restart
        self._http_client = http_client or self._default_http_client
        self._failure_count = 0
        self._backoff_seconds = 0
        self._running = False

    @staticmethod
    def _default_http_client(url: str, timeout: float) -> object:
        """默认 HTTP 客户端：使用 urllib 请求 health 接口。"""
        return urllib.request.urlopen(url, timeout=timeout)

    def _default_restart(self) -> None:
        """默认重启命令：执行配置中的 shell 命令。"""
        subprocess.run(
            self.config.restart_command,
            shell=True,
            check=False,
            capture_output=True,
        )

    def _check_health(self) -> bool:
        """请求 Server /health，返回是否健康。"""
        try:
            response = self._http_client(
                self.config.health_url,
                self.config.health_timeout_seconds,
            )
            return getattr(response, "status", None) == 200
        except Exception:  # noqa: BLE001
            return False

    def run(self) -> None:
        """启动监控循环，直到调用 stop() 或收到终止信号。"""
        self._running = True
        logger = get_logger("watchdog")
        logger.info(
            f"Watchdog 开始监控: url={self.config.health_url}, "
            f"interval={self.config.check_interval_seconds}s, "
            f"threshold={self.config.failure_threshold}"
        )

        while self._running:
            if self._check_health():
                if self._failure_count > 0 or self._backoff_seconds > 0:
                    logger.info("Server 恢复健康，失败计数与退避归零")
                self._failure_count = 0
                self._backoff_seconds = 0
            else:
                self._failure_count += 1
                logger.warning(
                    f"Server /health 检查失败，当前连续失败次数: {self._failure_count}"
                )

                if self._failure_count >= self.config.failure_threshold:
                    logger.error("Server 无响应，准备重启")
                    try:
                        self._restart_func()
                        logger.info("重启命令已执行")
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"重启命令执行失败: {exc}")
                        self._increase_backoff()
                    self._failure_count = 0

            sleep_seconds = self.config.check_interval_seconds + self._backoff_seconds
            time.sleep(sleep_seconds)

        logger.info("Watchdog 已停止")

    def _increase_backoff(self) -> None:
        """指数增加退避时间，直到最大值。"""
        if self._backoff_seconds == 0:
            self._backoff_seconds = self.config.check_interval_seconds
        else:
            self._backoff_seconds = min(
                self._backoff_seconds * 2,
                self.config.max_backoff_seconds,
            )

    def stop(self) -> None:
        """请求停止监控循环。"""
        self._running = False


def _handle_signal(runner: WatchdogRunner, signum: int, _frame: Optional[FrameType]) -> None:
    """信号处理：收到 SIGTERM/SIGINT 时优雅退出。"""
    logger = get_logger("watchdog")
    logger.info(f"收到信号 {signum}，准备优雅退出")
    runner.stop()


def main() -> int:
    """Watchdog 独立入口。"""
    setup_logging()
    logger = get_logger("watchdog")

    config = WatchdogConfig.from_env()
    if not config.enabled:
        logger.info("Watchdog 未启用（设置 CHAOSOPS_WATCHDOG_ENABLED=true 开启），退出")
        return 0

    runner = WatchdogRunner(config)
    signal.signal(signal.SIGTERM, lambda s, f: _handle_signal(runner, s, f))
    signal.signal(signal.SIGINT, lambda s, f: _handle_signal(runner, s, f))

    runner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
