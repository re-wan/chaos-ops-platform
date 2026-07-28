"""Agent 与 Server 通信的客户端。"""


import httpx


def register_agent(
    server_url: str,
    install_key: str,
    hostname: str,
    os: str,
    arch: str,
    version: str,
    timeout: float = 30.0,
) -> dict:
    """使用 Install Key 向 Server 注册，换取 Agent Token。

    Args:
        server_url: Server 地址，如 http://localhost:8000
        install_key: 一次性安装密钥
        hostname: 主机名
        os: 操作系统
        arch: 系统架构
        version: Agent 版本
        timeout: 请求超时时间

    Returns:
        Server 返回的注册响应字典
    """
    url = f"{server_url.rstrip('/')}/api/v1/agents/register"
    payload = {
        "install_key": install_key,
        "hostname": hostname,
        "os": os,
        "arch": arch,
        "version": version,
    }
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


def check_protected(
    server_url: str,
    agent_token: str,
    timeout: float = 10.0,
) -> dict:
    """测试 Agent Token 是否能访问受保护接口（调试用）。"""
    url = f"{server_url.rstrip('/')}/api/v1/agents/protected-test"
    headers = {"Authorization": f"Bearer {agent_token}"}
    with httpx.Client(timeout=timeout) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
