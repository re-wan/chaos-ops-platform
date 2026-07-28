"""Agent 端自愈任务执行器。

定期轮询 Server 拉取待执行的自愈任务，本地执行后上报结果。
MVP 阶段使用轮询拉取模式，Phase 2 可升级为 Server 主动推送。
"""

import logging
import time

import httpx

from agent.app.actions import builtin as actions_builtin
from agent.app.actions.custom_script import execute_custom_script

logger = logging.getLogger("agent.executor")

DEFAULT_POLL_INTERVAL_SECONDS = 10
DEFAULT_TASK_TIMEOUT_SECONDS = 120
# 任务结果上报失败重试：首次失败后最多重试次数，以及指数退避基数（秒）。
# 仅重试“上报”这一步，绝不重复执行动作，保证动作幂等。
DEFAULT_REPORT_RETRY_MAX = 3
DEFAULT_REPORT_BACKOFF_BASE_SECONDS = 2.0


class TaskExecutor:
    """Agent 自愈任务执行器。"""

    def __init__(
        self,
        server_url: str,
        agent_token: str,
        node_id: str,
        poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
        timeout_seconds: float = 10.0,
        report_retry_max: int = DEFAULT_REPORT_RETRY_MAX,
        report_backoff_base_seconds: float = DEFAULT_REPORT_BACKOFF_BASE_SECONDS,
    ):
        self.server_url = server_url.rstrip("/")
        self.agent_token = agent_token
        self.node_id = node_id
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.report_retry_max = report_retry_max
        self.report_backoff_base_seconds = report_backoff_base_seconds
        self.headers = {"Authorization": f"Bearer {agent_token}"}
        self.running = False

    def _get(self, path: str) -> dict:
        """发送 GET 请求。"""
        url = f"{self.server_url}{path}"
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json()

    def _post(self, path: str, payload: dict) -> dict:
        """发送 POST 请求。"""
        url = f"{self.server_url}{path}"
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(url, json=payload, headers=self.headers)
            response.raise_for_status()
            return response.json()

    def _execute_action(self, action_id: str, action_params: dict) -> dict:
        """根据 action_id 调用本地预置动作函数或自定义脚本沙箱。"""
        action_type = action_params.get("action_type")

        # 脚本类动作（custom_script / ai_generated_script）走沙箱执行
        if (
            action_type in ("custom_script", "ai_generated_script")
            or action_id == "run_script"
        ):
            return self._execute_custom_script(action_params)

        action_func = getattr(actions_builtin, action_id, None)
        if action_func is None:
            return {
                "success": False,
                "message": f"未知的动作: {action_id}",
                "output": "",
            }

        try:
            return action_func(**action_params)
        except TypeError as e:
            return {
                "success": False,
                "message": f"动作参数不匹配: {e}",
                "output": "",
            }
        except Exception as e:  # noqa: BLE001
            return {
                "success": False,
                "message": f"动作执行异常: {e}",
                "output": "",
            }

    def _execute_custom_script(self, action_params: dict) -> dict:
        """执行自定义脚本并标准化返回结果。"""
        script_content = action_params.get("script_content")
        script_hash = action_params.get("script_hash")
        interpreter = action_params.get("interpreter", "bash")
        args = action_params.get("args") or action_params.get("action_params") or {}
        timeout = action_params.get("timeout_seconds")

        if not script_content:
            return {
                "success": False,
                "message": "缺少 script_content",
                "output": "",
            }
        if not script_hash:
            return {
                "success": False,
                "message": "缺少 script_hash",
                "output": "",
            }

        try:
            result = execute_custom_script(
                script_content=script_content,
                script_hash=script_hash,
                interpreter=interpreter,
                args=args,
                timeout=timeout,
            )
            # 统一返回结构：stdout/stderr/message/output
            return {
                "success": result.get("success", False),
                "message": result.get("message", ""),
                "output": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "exit_code": result.get("exit_code", -1),
            }
        except Exception as e:  # noqa: BLE001
            logger.exception("自定义脚本执行异常")
            return {
                "success": False,
                "message": f"自定义脚本执行异常: {e}",
                "output": "",
            }

    def _pull_tasks(self) -> list[dict]:
        """从 Server 拉取待执行任务。"""
        try:
            data = self._get("/api/v1/agent/tasks/pending")
            return data.get("tasks", [])
        except httpx.HTTPStatusError as e:
            logger.warning(f"拉取任务失败: {e.response.status_code}")
            return []
        except Exception as e:  # noqa: BLE001
            logger.warning(f"拉取任务异常: {e}")
            return []

    def _report_result(self, task_id: str, result: dict) -> None:
        """上报任务执行结果，失败时按指数退避重试。

        仅重试“上报”这一步，不重复执行动作；最终失败则记日志并返回，
        不阻塞 Agent 主循环（服务端有 running 卡死清扫兜底）。
        """
        payload = {
            "success": result.get("success", False),
            "output": result.get("output", ""),
            "message": result.get("message", ""),
            "error_message": result.get("error_message", ""),
        }
        max_attempts = self.report_retry_max + 1
        for attempt in range(1, max_attempts + 1):
            try:
                self._post(f"/api/v1/agent/tasks/{task_id}/result", payload)
                logger.info(f"任务结果上报成功: {task_id}")
                return
            except Exception as e:  # noqa: BLE001
                if attempt < max_attempts:
                    backoff = self.report_backoff_base_seconds * (
                        2 ** (attempt - 1)
                    )
                    logger.warning(
                        f"任务结果上报失败，{backoff:.1f}s 后重试 "
                        f"({attempt}/{self.report_retry_max}): {task_id}, error={e}"
                    )
                    time.sleep(backoff)
                else:
                    logger.warning(
                        f"任务结果上报最终失败，已达最大重试次数 "
                        f"({self.report_retry_max}): {task_id}, error={e}"
                    )

    def _process_tasks(self, tasks: list[dict]) -> None:
        """执行并上报所有任务。"""
        for task in tasks:
            task_id = task["task_id"]
            task_type = task.get("task_type", "heal")
            action_id = task["action_id"]
            action_params = task.get("action_params", {})

            # 将 Server 下发的顶层字段合并到 action_params，便于 _execute_action 识别
            for key in ("action_type", "script_content", "script_hash", "interpreter"):
                if key in task and key not in action_params:
                    action_params[key] = task[key]

            logger.info(
                f"执行任务: task_id={task_id}, type={task_type}, "
                f"action_id={action_id}, params={action_params}"
            )

            if task_type == "remote":
                # 远程执行直接调用本地动作，不需要查询 HealAction
                result = self._execute_action(action_id, action_params)
            else:
                # heal 类型保持现有逻辑
                result = self._execute_action(action_id, action_params)

            self._report_result(task_id, result)

    def run_once(self) -> None:
        """执行一轮拉取-执行-上报。"""
        tasks = self._pull_tasks()
        if tasks:
            self._process_tasks(tasks)

    def run(self) -> None:
        """启动轮询循环。"""
        logger.info(
            f"任务执行器启动，节点: {self.node_id}, "
            f"轮询间隔: {self.poll_interval_seconds}s"
        )
        self.running = True

        try:
            while self.running:
                self.run_once()
                time.sleep(self.poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("收到退出信号")
        finally:
            self.running = False
            logger.info("任务执行器已停止")

    def stop(self) -> None:
        """停止轮询循环。"""
        self.running = False


def create_executor_from_config(config: dict) -> TaskExecutor:
    """根据 Agent 配置创建任务执行器。

    上报重试配置优先读取大写键（与环境变量命名一致），缺省回退到
    snake_case JSON 键，再缺省使用内置默认值。
    """
    return TaskExecutor(
        server_url=config["server_url"],
        agent_token=config["agent_token"],
        node_id=config["node_id"],
        report_retry_max=int(
            config.get(
                "AGENT_REPORT_RETRY_MAX",
                config.get("report_retry_max", DEFAULT_REPORT_RETRY_MAX),
            )
        ),
        report_backoff_base_seconds=float(
            config.get(
                "AGENT_REPORT_BACKOFF_BASE_SECONDS",
                config.get(
                    "report_backoff_base_seconds",
                    DEFAULT_REPORT_BACKOFF_BASE_SECONDS,
                ),
            )
        ),
    )
