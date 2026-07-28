"""ChaosOps 本地 Agent 入口。

本地 Agent 部署在 Server 同一台机器上，反向监控 Server 自身。
主要职责：
    - 定期请求 Server /health
    - 连续 N 次失败时触发 restart_server 动作
    - 执行本地专属维护动作
    - 父进程（Server）看门狗：Server 被 kill -9 时不做孤儿，自行退出
"""

import argparse
import logging
import os
import threading

import httpx

# 与 main.py 保持一致的绝对包路径：`python -m agent.app.local_main` 运行时
# cwd（server 安装目录）下 `app` 解析为后端包，写 `from app.local_executor`
# 会 ModuleNotFoundError。
from agent.app.local_executor import LocalExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [local_agent] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("local_agent")

# 父进程看门狗检查间隔（秒）。独立于采集间隔：采集间隔可能较长（默认 10s），
# 看门狗需更快地发现 Server 死亡，避免新 Server 接管后出现双 Agent 窗口过长。
PPID_CHECK_INTERVAL = 5.0


class LocalAgent:
    """本地 Agent 实现。"""

    def __init__(
        self,
        server_url: str,
        agent_token: str,
        check_interval: int = 10,
        health_timeout: float = 5.0,
        max_failures: int = 3,
    ):
        self.server_url = server_url.rstrip("/")
        self.agent_token = agent_token
        self.check_interval = check_interval
        self.health_timeout = health_timeout
        self.max_failures = max_failures
        self.consecutive_failures = 0
        self.executor = LocalExecutor(server_url, agent_token)
        # 退出事件：父进程看门狗置位后主循环有序退出（stop 采集 → 正常返回 →
        # 解释器退出时自动 flush 日志/关闭连接，等价于 sys.exit(0) 但更安全）。
        self._stop_event = threading.Event()
        # 记录启动时的父进程 PID，作为看门狗基准。
        self._original_ppid = os.getppid()

    def check_health(self) -> bool:
        """请求 Server /health，返回是否成功。"""
        try:
            response = httpx.get(
                f"{self.server_url}/health",
                timeout=self.health_timeout,
            )
            if response.status_code == 200:
                self.consecutive_failures = 0
                logger.debug("Server /health 检查通过")
                return True
        except Exception as e:
            logger.warning(f"Server /health 检查失败: {e}")

        self.consecutive_failures += 1
        return False

    def _parent_watchdog(self) -> None:
        """父进程看门狗：Server 死亡（ppid 变化）时自行退出，不做孤儿进程。

        本地 Agent 是 Server 的 subprocess.Popen 子进程。Server 被 kill -9 时
        来不及 terminate 子进程，Agent 会变孤儿（ppid 重挂）；新 Server 接管后
        再起一个 Agent → 双 Agent 重复采集上报同一节点（H2 实测坐实）。

        为什么用 ppid 自检而不是 PR_SET_PDEATHSIG / preexec_fn：
        preexec_fn 在多线程进程（uvicorn 运行中的 server）fork 子进程时有
        Python 官方文档明确警告的死锁风险（fork 时其他线程持有的锁在子进程
        中永远不会被释放）；且 PDEATHSIG 是 Linux 专属、信号竞态边界多。
        ppid 周期自检跨平台、可单测、对父进程零侵入、无任何副作用。

        fail-safe 边界：os.getppid() 本身异常（极罕见）时**不自杀**——宁存活
        勿误杀，与项目整体 fail-safe 哲学一致；正常 terminate 路径（Server
        优雅退出先 terminate Agent）ppid 不变，不会误触发。
        """
        while not self._stop_event.wait(PPID_CHECK_INTERVAL):
            try:
                current_ppid = os.getppid()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"父进程检查异常（忽略，不退出）: {e}")
                continue
            if current_ppid != self._original_ppid:
                logger.warning(
                    f"父进程已退出（ppid {self._original_ppid}→{current_ppid}），"
                    "本地 Agent 自行退出（避免成为孤儿进程）"
                )
                self._stop_event.set()
                return

    def run(self) -> None:
        """启动本地 Agent 主循环。"""
        logger.info(f"本地 Agent 启动，监控 Server: {self.server_url}")

        # 父进程看门狗（daemon 线程：主循环退出后随进程消亡，无需 join）
        threading.Thread(
            target=self._parent_watchdog,
            name="parent-watchdog",
            daemon=True,
        ).start()

        while not self._stop_event.is_set():
            healthy = self.check_health()

            if not healthy and self.consecutive_failures >= self.max_failures:
                logger.error(
                    f"连续 {self.max_failures} 次 /health 检查失败，触发 restart_server"
                )
                try:
                    self.executor.restart_server()
                except Exception as e:
                    logger.error(f"restart_server 执行失败: {e}")
                # 触发重启后重置计数，避免短时间重复触发
                self.consecutive_failures = 0

            # 用 Event.wait 替代 time.sleep：看门狗置位后立即醒来退出，
            # 不必等满整个采集间隔。
            self._stop_event.wait(self.check_interval)

        logger.info("本地 Agent 已停止")


def main() -> None:
    parser = argparse.ArgumentParser(description="ChaosOps Local Agent")
    parser.add_argument("--server-url", required=True, help="Server URL")
    parser.add_argument("--agent-token", required=True, help="Agent Token")
    parser.add_argument("--check-interval", type=int, default=10, help="健康检查间隔（秒）")
    parser.add_argument("--health-timeout", type=float, default=5.0, help="健康检查超时（秒）")
    args = parser.parse_args()

    agent = LocalAgent(
        server_url=args.server_url,
        agent_token=args.agent_token,
        check_interval=args.check_interval,
        health_timeout=args.health_timeout,
    )
    agent.run()
    # run() 正常返回（看门狗触发有序退出）→ 进程以 exit code 0 退出，
    # 解释器收尾自动 flush 日志，无需显式 sys.exit。


if __name__ == "__main__":
    main()
