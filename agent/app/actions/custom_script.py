"""Agent 端自定义脚本沙箱执行器。

提供 SandboxRunner 抽象接口，默认实现基于黑名单 + 资源限制。
MVP 阶段为轻量级沙箱，生产环境建议配合容器 / seccomp / AppArmor 使用。
"""

import hashlib
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from agent.app.config import get_custom_script_config

logger = logging.getLogger("agent.actions.custom_script")


# 默认返回结构
DEFAULT_RESULT = {
    "success": False,
    "exit_code": -1,
    "stdout": "",
    "stderr": "",
    "message": "",
}


class SandboxError(Exception):
    """沙箱执行异常。"""


class SandboxRunner(ABC):
    """脚本沙箱执行器抽象基类。

    预留更强隔离实现（如 DockerRunner、FirejailRunner）的扩展口。
    """

    @abstractmethod
    def execute(
        self,
        script_content: str,
        interpreter: str,
        args: Optional[dict],
        timeout: int,
    ) -> dict:
        """执行脚本并返回标准结果。

        Args:
            script_content: 脚本内容（已校验 hash 前不可信任）
            interpreter: 解释器，支持 bash / python / python3
            args: 动作参数字典
            timeout: 执行超时秒数

        Returns:
            {"success": bool, "exit_code": int, "stdout": str, "stderr": str, "message": str}
        """
        ...


class BlacklistSandboxRunner(SandboxRunner):
    """基于黑名单 + 资源限制的轻量级沙箱。

    安全说明：
    - 黑名单无法防御所有恶意绕过（如 base64 编码、字符串拼接、下载后执行等）。
    - 本实现仅作为 MVP 轻量级隔离，关键生产环境应使用容器级隔离。
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = config or get_custom_script_config()
        self.blocked_patterns = [
            re.compile(p, re.IGNORECASE) for p in self.config.get("blocked_patterns", [])
        ]
        self.allowed_env_vars = set(
            self.config.get("allowed_env_vars", ["PATH", "HOME", "LANG", "LC_ALL", "TZ", "USER", "SHELL", "TERM"])
        )
        self.work_dir = Path(self.config.get("work_dir", "/tmp/chaosops-sandbox"))
        self.max_output_bytes = self.config.get("max_output_bytes", 50 * 1024)

    def _validate_script_content(self, script_content: str) -> None:
        """静态黑名单扫描，命中则抛异常。"""
        for line_no, line in enumerate(script_content.splitlines(), start=1):
            for pattern in self.blocked_patterns:
                if pattern.search(line):
                    raise SandboxError(
                        f"脚本内容被黑名单拦截 (line {line_no}): {line.strip()[:80]}"
                    )

    def _filter_env(self) -> dict:
        """只保留白名单内的环境变量。"""
        return {k: v for k, v in os.environ.items() if k in self.allowed_env_vars}

    def _prepare_work_dir(self) -> Path:
        """创建隔离临时工作目录。"""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="chaosops-sandbox-", dir=self.work_dir))
        return work_dir

    def _resolve_interpreter(self, interpreter: str) -> str:
        """将 interpreter 名称解析为可执行文件路径。"""
        interpreters = {
            "bash": "bash",
            "python": "python3",
            "python3": "python3",
        }
        name = interpreters.get(interpreter, interpreter)
        resolved = shutil.which(name)
        if resolved is None:
            raise SandboxError(f"解释器未找到: {interpreter}")
        return resolved

    def _set_resource_limits(self) -> None:
        """Linux 下设置子进程资源限制。"""
        if platform.system() != "Linux":
            return
        try:
            import resource

            max_cpu = self.config.get("max_cpu_seconds", 30)
            max_mem_mb = self.config.get("max_memory_mb", 256)
            max_file_mb = self.config.get("max_file_size_mb", 64)
            max_proc = self.config.get("max_processes", 64)

            # CPU 时间（硬限制和软限制相同）
            resource.setrlimit(resource.RLIMIT_CPU, (max_cpu, max_cpu))
            # 虚拟内存地址空间
            resource.setrlimit(
                resource.RLIMIT_AS,
                (max_mem_mb * 1024 * 1024, max_mem_mb * 1024 * 1024),
            )
            # 可创建的最大文件
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (max_file_mb * 1024 * 1024, max_file_mb * 1024 * 1024),
            )
            # 子进程数：RLIMIT_NPROC 按真实 UID 计数，测试/开发环境用户可能已有大量进程，
            # 若当前进程数已接近限制则跳过，避免所有 fork 失败。
            if self._can_apply_nproc_limit(max_proc):
                resource.setrlimit(resource.RLIMIT_NPROC, (max_proc, max_proc))
            else:
                logger.warning(
                    f"当前 UID 进程数接近或超过 NPROC 限制 {max_proc}，跳过子进程数限制"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"设置资源限制失败: {exc}")

    def _can_apply_nproc_limit(self, max_proc: int) -> bool:
        """检查是否可以安全应用 NPROC 限制。"""
        try:
            import os

            uid = os.getuid()
            count = 0
            for entry in os.scandir("/proc"):
                if not entry.name.isdigit():
                    continue
                try:
                    stat = os.stat(entry.path)
                    if stat.st_uid == uid:
                        count += 1
                except OSError:
                    continue
            # 预留一些缓冲
            return count + 5 < max_proc
        except Exception:  # noqa: BLE001
            return False

    def _truncate_output(self, text: str) -> str:
        """截断过长的输出。"""
        if len(text) > self.max_output_bytes:
            truncated = text[: self.max_output_bytes]
            truncated += "\n[chaosops] 输出已截断"
            return truncated
        return text

    def execute(
        self,
        script_content: str,
        interpreter: str = "bash",
        args: Optional[dict] = None,
        timeout: int = 60,
    ) -> dict:
        """执行脚本内容，返回标准结构。"""
        args = args or {}
        result = DEFAULT_RESULT.copy()
        work_dir: Optional[Path] = None

        try:
            self._validate_script_content(script_content)
        except SandboxError as exc:
            result["message"] = str(exc)
            return result

        try:
            work_dir = self._prepare_work_dir()
            script_path = work_dir / "script"
            script_path.write_text(script_content, encoding="utf-8")

            interpreter_path = self._resolve_interpreter(interpreter)
            command = [interpreter_path, str(script_path)]
            # bash 脚本需要显式通过 bash 执行；python 脚本同理
            if interpreter == "bash":
                command = [interpreter_path, str(script_path)]

            env = self._filter_env()
            # 注入动作参数作为环境变量，供脚本使用
            for key, value in args.items():
                env[f"CHAOSOPS_ARG_{key}"] = str(value)

            kwargs = {
                "cwd": str(work_dir),
                "env": env,
                "timeout": timeout,
                "capture_output": True,
                "text": True,
            }
            if platform.system() == "Linux":
                kwargs["preexec_fn"] = self._set_resource_limits

            logger.info(
                f"沙箱执行脚本: interpreter={interpreter}, timeout={timeout}, "
                f"work_dir={work_dir}"
            )
            proc = subprocess.run(command, **kwargs)

            result.update({
                "success": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": self._truncate_output(proc.stdout or ""),
                "stderr": self._truncate_output(proc.stderr or ""),
                "message": "脚本执行完成" if proc.returncode == 0 else f"脚本退出码: {proc.returncode}",
            })
        except subprocess.TimeoutExpired as exc:
            result.update({
                "exit_code": -1,
                "stdout": self._truncate_output(exc.stdout or ""),
                "stderr": self._truncate_output(exc.stderr or ""),
                "message": f"脚本执行超时（{timeout} 秒）",
            })
        except SandboxError as exc:
            result["message"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("脚本执行异常")
            result["message"] = f"脚本执行异常: {exc}"
        finally:
            if work_dir is not None and work_dir.exists():
                try:
                    shutil.rmtree(work_dir)
                except OSError as exc:
                    logger.warning(f"清理沙箱工作目录失败: {exc}")

        return result


def _compute_script_hash(script_content: str) -> str:
    """计算脚本内容 SHA256 hash。"""
    return hashlib.sha256(script_content.encode("utf-8")).hexdigest()


def execute_custom_script(
    script_content: str,
    script_hash: str,
    interpreter: str = "bash",
    args: Optional[dict] = None,
    timeout: Optional[int] = None,
    runner: Optional[SandboxRunner] = None,
) -> dict:
    """执行自定义脚本入口。

    1. 校验本地 hash 是否与内容匹配
    2. 调用沙箱执行器运行脚本
    """
    config = get_custom_script_config()
    if not config.get("enabled", True):
        return {
            **DEFAULT_RESULT,
            "message": "自定义脚本执行已被禁用",
        }

    expected_hash = _compute_script_hash(script_content)
    if expected_hash != script_hash:
        logger.warning(
            f"脚本 hash 不匹配: expected={expected_hash}, received={script_hash}"
        )
        return {
            **DEFAULT_RESULT,
            "message": "脚本 hash 校验失败，拒绝执行",
        }

    if timeout is None:
        timeout = config.get("timeout_seconds", 60)

    if runner is None:
        # 批 15：可选沙箱层——按配置 sandbox_runner 选择，docker 不可用自动降级
        # （懒加载避免与 docker_runner 循环导入）。默认 blacklist，行为不变。
        from agent.app.actions.docker_runner import build_sandbox_runner

        runner = build_sandbox_runner(config)

    return runner.execute(
        script_content=script_content,
        interpreter=interpreter,
        args=args,
        timeout=timeout,
    )
