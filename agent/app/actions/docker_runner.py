"""Docker 容器级沙箱执行器（可选增强层，批 15）。

与 ``BlacklistSandboxRunner`` 相同的执行接口，隔离强度更高：

- ``--network none``：容器内完全断网（黑名单沙箱不隔离网络）；
- ``--read-only``：根文件系统只读，无可写挂载；
- ``--memory`` / ``--cpus`` / ``--pids-limit``：资源限额；
- ``--rm``：执行结束自动销毁容器；
- 脚本内容经 **stdin** 传入，不进入命令行、不落宿主机文件；
- 超时强杀：``docker kill`` 兜底，确保容器不留存。

安全红线：
- 不使用 ``--privileged``；不挂载 docker.sock；不挂载任何宿主机路径；
- 镜像名、资源限额只来自配置（运维可信输入），脚本/参数绝不拼进 shell 字符串；
- 动作参数经 ``--env KEY=VALUE``（argv 单元素，无 shell 解析）传入，
  环境变量名做白名单校验，拒绝非法字符。

启用两道闸（缺一即降级 ``BlacklistSandboxRunner``，告警 30s 节流）：
1. 配置开关 ``custom_script.sandbox_runner = "docker"``（默认 ``blacklist``）；
2. 运行时可用性探测：docker 命令存在、``docker info`` 可通、配置镜像本地存在
   （探测结果缓存 60s，避免每次执行都打 daemon）。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from typing import Optional

from agent.app.actions.custom_script import (
    DEFAULT_RESULT,
    BlacklistSandboxRunner,
    SandboxError,
    SandboxRunner,
)
from agent.app.config import get_custom_script_config

logger = logging.getLogger("agent.actions.docker_runner")

# 默认镜像：常用 python slim（同时自带 bash）。部署文档要求运维预置到本地，
# 沙箱不会主动拉取镜像（docker run 缺镜像会失败，探测阶段即降级）。
DEFAULT_DOCKER_IMAGE = "python:3.12-slim"

# 降级告警节流：同一原因每 30s 至多一条（仿照 api_auth._warn_degraded_rate_limit）。
_DEGRADED_WARN_INTERVAL_SECONDS = 30.0
_warn_lock = threading.Lock()
_warn_last: dict[str, float] = {}

# 可用性探测缓存：避免每次执行脚本都调用 docker info / image inspect。
_PROBE_TTL_SECONDS = 60.0
_probe_lock = threading.Lock()
_probe_cache: dict[str, tuple[float, bool, str]] = {}

# 容器内最小 PATH（不继承宿主机环境）。
_CONTAINER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# 环境变量名白名单：仅字母数字下划线，防止 --env 注入。
_SAFE_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_INTERPRETER_COMMANDS = {
    "bash": ["bash"],
    "python": ["python3", "-"],
    "python3": ["python3", "-"],
}


def _warn_degraded_throttled(reason: str) -> None:
    """降级告警（节流：同一原因每 30s 至多一条，避免刷日志）。"""
    now = time.monotonic()
    with _warn_lock:
        last = _warn_last.get(reason, 0.0)
        if now - last < _DEGRADED_WARN_INTERVAL_SECONDS:
            return
        _warn_last[reason] = now
    logger.warning(
        f"DockerSandboxRunner 不可用（{reason}），已降级为 BlacklistSandboxRunner"
    )


def _probe_docker(image: str) -> tuple[bool, str]:
    """实际探测 docker 可用性与镜像存在性，返回 (可用, 不可用原因)。"""
    if shutil.which("docker") is None:
        return False, "未找到 docker 命令"
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"docker info 探测失败: {exc}"
    if proc.returncode != 0:
        return False, "docker daemon 不可用"
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"镜像探测失败: {exc}"
    if proc.returncode != 0:
        return False, f"镜像本地不存在: {image}（需运维预置，沙箱不自动拉取）"
    return True, ""


def docker_available(image: str) -> tuple[bool, str]:
    """带 60s 缓存的可用性探测，返回 (可用, 不可用原因)。"""
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(image)
        if cached is not None and now - cached[0] < _PROBE_TTL_SECONDS:
            return cached[1], cached[2]
    ok, reason = _probe_docker(image)
    with _probe_lock:
        _probe_cache[image] = (now, ok, reason)
    return ok, reason


class DockerSandboxRunner(SandboxRunner):
    """Docker 容器级沙箱：一次性容器内执行脚本，断网 + 只读 FS + 资源限额。"""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or get_custom_script_config()
        self.image = str(self.config.get("docker_image", DEFAULT_DOCKER_IMAGE))
        self.memory_mb = int(self.config.get("max_memory_mb", 256))
        self.cpus = self.config.get("docker_cpus", 1.0)
        self.pids_limit = int(self.config.get("docker_pids_limit", 64))
        self.max_output_bytes = self.config.get("max_output_bytes", 50 * 1024)
        # 深度防御：容器隔离之上仍保留黑名单静态扫描（与黑名单沙箱同源配置）。
        self.blocked_patterns = [
            re.compile(p, re.IGNORECASE) for p in self.config.get("blocked_patterns", [])
        ]

    def _validate_script_content(self, script_content: str) -> None:
        """静态黑名单扫描（深度防御，命中即拒绝）。"""
        for line_no, line in enumerate(script_content.splitlines(), start=1):
            for pattern in self.blocked_patterns:
                if pattern.search(line):
                    raise SandboxError(
                        f"脚本内容被黑名单拦截 (line {line_no}): {line.strip()[:80]}"
                    )

    def _build_command(self, container_name: str, interpreter: str, args: dict) -> list:
        """构造 docker run 命令（list 形式，无 shell 拼接）。

        安全要点：脚本内容不进命令行（走 stdin）；镜像名/限额来自配置；
        不使用 privileged、不挂载任何卷（无 -v）、网络隔离、根 FS 只读。
        """
        interpreter_cmd = _INTERPRETER_COMMANDS.get(interpreter)
        if interpreter_cmd is None:
            raise SandboxError(f"不支持的解释器: {interpreter}")

        cmd = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--memory",
            f"{self.memory_mb}m",
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids_limit),
            "--env",
            f"PATH={_CONTAINER_PATH}",
        ]
        # 注入动作参数为环境变量（与黑名单沙箱语义一致：CHAOSOPS_ARG_<key>）。
        # key 做白名单校验；value 作为 argv 单元素传入，无 shell 解析风险。
        for key, value in args.items():
            if not _SAFE_ENV_KEY.match(str(key)):
                raise SandboxError(f"非法的参数名（环境变量注入被拒绝）: {key}")
            cmd += ["--env", f"CHAOSOPS_ARG_{key}={value}"]

        cmd += [self.image, *interpreter_cmd]
        return cmd

    def _truncate_output(self, text: str) -> str:
        """截断过长的输出（与黑名单沙箱语义一致）。"""
        if len(text) > self.max_output_bytes:
            truncated = text[: self.max_output_bytes]
            truncated += "\n[chaosops] 输出已截断"
            return truncated
        return text

    def _force_kill(self, container_name: str, proc: subprocess.Popen) -> None:
        """超时强杀：docker kill 兜底销毁容器，再杀本地 docker CLI 进程。"""
        try:
            subprocess.run(
                ["docker", "kill", container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"docker kill 失败（容器 {container_name}）: {exc}")
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError as exc:
                logger.warning(f"终止本地 docker 进程失败: {exc}")

    def execute(
        self,
        script_content: str,
        interpreter: str = "bash",
        args: Optional[dict] = None,
        timeout: int = 60,
    ) -> dict:
        """在一次性 Docker 容器内执行脚本，返回与黑名单沙箱相同的标准结构。"""
        args = args or {}
        result = DEFAULT_RESULT.copy()

        try:
            self._validate_script_content(script_content)
        except SandboxError as exc:
            result["message"] = str(exc)
            return result

        container_name = f"chaosops-sandbox-{uuid.uuid4().hex[:12]}"
        try:
            command = self._build_command(container_name, interpreter, args)
        except SandboxError as exc:
            result["message"] = str(exc)
            return result

        proc: Optional[subprocess.Popen] = None
        try:
            logger.info(
                f"Docker 沙箱执行脚本: image={self.image}, interpreter={interpreter}, "
                f"timeout={timeout}, container={container_name}"
            )
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate(input=script_content, timeout=timeout)
            result.update({
                "success": proc.returncode == 0,
                "exit_code": proc.returncode if proc.returncode is not None else -1,
                "stdout": self._truncate_output(stdout or ""),
                "stderr": self._truncate_output(stderr or ""),
                "message": (
                    "脚本执行完成"
                    if proc.returncode == 0
                    else f"脚本退出码: {proc.returncode}"
                ),
            })
        except subprocess.TimeoutExpired:
            # 超时强杀：docker kill 兜底（message 含“超时”，server 侧据此判 timed_out）
            if proc is not None:
                self._force_kill(container_name, proc)
            stdout, stderr = "", ""
            if proc is not None:
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except Exception:  # noqa: BLE001
                    stdout, stderr = "", ""
            result.update({
                "exit_code": -1,
                "stdout": self._truncate_output(stdout or ""),
                "stderr": self._truncate_output(stderr or ""),
                "message": f"脚本执行超时（{timeout} 秒）",
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("Docker 沙箱执行异常")
            if proc is not None and proc.poll() is None:
                self._force_kill(container_name, proc)
            result["message"] = f"脚本执行异常: {exc}"

        return result


def build_sandbox_runner(config: Optional[dict] = None) -> SandboxRunner:
    """沙箱 Runner 工厂：按配置选择，docker 不可用时自动降级黑名单沙箱。

    两道闸：① ``sandbox_runner == "docker"``；② 运行时探测通过。
    任一不满足 → ``BlacklistSandboxRunner``（降级告警 30s 节流）。
    """
    config = config or get_custom_script_config()
    if config.get("sandbox_runner", "blacklist") != "docker":
        return BlacklistSandboxRunner(config)

    image = str(config.get("docker_image", DEFAULT_DOCKER_IMAGE))
    ok, reason = docker_available(image)
    if not ok:
        _warn_degraded_throttled(reason)
        return BlacklistSandboxRunner(config)
    return DockerSandboxRunner(config)
