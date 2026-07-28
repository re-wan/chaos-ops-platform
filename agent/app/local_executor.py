"""本地 Agent 专属动作执行器。"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger("local_agent.executor")


class LocalExecutor:
    """本地 Agent 专用动作执行器。

    负责调用 Server 的本地专属动作接口（如 restart_server）。
    """

    def __init__(self, server_url: str, agent_token: str, timeout: float = 10.0):
        self.server_url = server_url.rstrip("/")
        self.agent_token = agent_token
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {agent_token}"}

    def _post_action(self, action: str, params: Optional[dict] = None) -> dict:
        """向 Server 发送本地动作请求。"""
        url = f"{self.server_url}/api/v1/agents/actions/execute"
        payload = {"action": action}
        if params:
            payload["params"] = params

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(url, json=payload, headers=self.headers)
            response.raise_for_status()
            return response.json()

    def restart_server(self) -> dict:
        """触发 Server 重启动作。"""
        logger.warning("执行 restart_server 动作")
        return self._post_action("restart_server")

    def cleanup_logs(self) -> dict:
        """触发日志清理动作。"""
        logger.info("执行 cleanup_logs 动作")
        return self._post_action("cleanup_logs")

    def vacuum_database(self) -> dict:
        """触发数据库 VACUUM 动作。"""
        logger.info("执行 vacuum_database 动作")
        return self._post_action("vacuum_database")
