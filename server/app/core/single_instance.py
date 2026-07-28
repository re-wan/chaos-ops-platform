"""单实例守卫：保证多 worker 部署下后台任务只运行一份。

典型场景：Server 以多进程（gunicorn/uvicorn --workers N）运行时，备份、告警检测、
通知重试、本地 Agent 等后台任务若在 N 个 worker 中各跑一份，会造成重复执行与资源争抢。

机制：非阻塞独占文件锁（``fcntl.flock``）。锁文件仅作为锁载体，不持久保存任何状态；
进程退出（正常或崩溃）时操作系统自动回收文件描述符与锁，容器重启不会残留死锁。

退化策略（fail-safe，宁运行勿误杀）：
- 锁被其他进程持有 -> 返回未获取（本进程作为纯 HTTP worker，不跑后台任务）。
- 锁机制本身不可用（非 Linux/无 fcntl/取锁异常）-> 降级为"单进程行为"（视为已获取），
  并打 warning，避免把全部后台任务误杀到 0 份。
"""

import os
from pathlib import Path
from typing import Optional

from app.core.logger import get_logger

logger = get_logger("single_instance")

try:
    import fcntl  # type: ignore[import-not-found]

    _HAS_FCNTL = True
except ImportError:  # 非 Linux 平台
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False


def _default_lock_dir() -> Path:
    """返回默认锁目录：``CHAOSOPS_LOCK_DIR`` 或项目根 ``data/locks``。"""
    env = os.environ.get("CHAOSOPS_LOCK_DIR")
    if env:
        return Path(env)
    # backend/app/core/single_instance.py -> 项目根（parents[3]）
    project_root = Path(__file__).resolve().parents[3]
    return project_root / "data" / "locks"


class SingleInstanceGuard:
    """非阻塞独占文件锁守卫。

    Args:
        name: 守卫名称（决定锁文件名），仅允许字母/数字/._-。
        lock_dir: 锁目录，默认 ``data/locks``（可用 ``CHAOSOPS_LOCK_DIR`` 覆盖）。
    """

    def __init__(self, name: str, lock_dir: Optional[os.PathLike] = None):
        if not name or any(c in name for c in "/\\"):
            raise ValueError(f"非法的守卫名称: {name!r}")
        self.name = name
        self._lock_dir = Path(lock_dir) if lock_dir else _default_lock_dir()
        self._lock_path = self._lock_dir / f"{name}.lock"
        self._fd: Optional[int] = None
        self._held: bool = False

    @property
    def acquired(self) -> bool:
        """当前对象是否持有（或降级持有）领导权。"""
        return self._held

    def acquire(self) -> bool:
        """尝试获取领导权（非阻塞）。成功返回 True，被他人持有返回 False。

        幂等：已持有时直接返回 True。
        """
        if self._held:
            return True

        # 显式标记的非 leader worker（如 gunicorn 按 worker 注入 SERVER_WORKER_ID）
        # 直接放弃取锁，减少无谓竞争；未设置时一律走文件锁（权威机制）。
        worker_id = os.environ.get("SERVER_WORKER_ID")
        if worker_id not in (None, "", "0"):
            logger.info(
                f"非 leader worker (SERVER_WORKER_ID={worker_id})，跳过后台任务: {self.name}"
            )
            return False

        if not _HAS_FCNTL:
            logger.warning(
                f"当前平台无 fcntl，单实例守卫 {self.name} 降级为单进程行为（视为已获取）"
            )
            self._held = True
            return True

        try:
            self._lock_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                # 被其他进程持有：本进程作为纯 HTTP worker
                os.close(fd)
                return False
            # 写入 holder 信息，便于排查（非锁语义必需）
            try:
                os.ftruncate(fd, 0)
                os.write(fd, f"pid={os.getpid()} name={self.name}\n".encode("utf-8"))
            except OSError:
                pass
            self._fd = fd
            self._held = True
            return True
        except OSError as exc:
            # 机制异常：降级为单进程行为，宁可运行也不误杀全部后台任务
            logger.warning(
                f"单实例守卫 {self.name} 取锁异常，降级为单进程行为: {exc}"
            )
            self._held = True
            return True

    def release(self) -> None:
        """释放领导权（解锁并关闭文件描述符）。幂等。"""
        fd = self._fd
        self._fd = None
        self._held = False
        if fd is None:
            return
        if _HAS_FCNTL:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "SingleInstanceGuard":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False
