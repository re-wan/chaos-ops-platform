"""自愈执行 5 分钟去重（Phase 3 Step 05，对齐 PERFORMANCE_DESIGN §4.3）。

同一 (node_id, action_id) 的自愈任务在去重窗口（默认 300s）内不重复下发，
避免告警风暴期间对同一节点同一动作反复执行造成冲突或副作用放大。

后端选择：

- Redis 可用（任务队列异步模式）→ 使用带 TTL 的 Redis key，多 worker 间共享，
  保证分布式去重一致。
- Redis 不可用 → 进程内字典兜底（仅单进程有效）。

启用策略（``HEAL_DEDUP_ENABLED``）：

- ``auto``（默认）：仅当 Redis 可用（任务队列异步）时启用。单进程/测试环境
  关闭，避免改变既有 firing → task 的测试预期（串行已由
  ``_has_running_task_for_node_action`` 保证）。
- ``on`` / ``off``：强制启用/禁用。
"""

from __future__ import annotations

import threading
import time

from app.core.logger import get_logger

logger = get_logger("services.heal_dedup")

_mem_lock = threading.Lock()
# key -> 过期时间（time.monotonic）
_mem_recent: dict[str, float] = {}

_KEY_PREFIX = "chaosops:heal:dedup"


def _key(node_id: str, action_id: str) -> str:
    return f"{_KEY_PREFIX}:{node_id}:{action_id}"


def is_dedup_enabled() -> bool:
    """解析去重开关（auto 时跟随 Redis 可用性）。"""
    from app.core.config import settings

    mode = (settings.HEAL_DEDUP_ENABLED or "auto").lower().strip()
    if mode == "on":
        return True
    if mode == "off":
        return False
    # auto：仅 Redis 异步模式启用
    from app.core.task_queue import get_task_queue

    return get_task_queue().is_async()


def _window_seconds() -> int:
    from app.core.config import settings

    return max(0, int(settings.HEAL_DEDUP_WINDOW_SECONDS))


def should_dedup(node_id: str, action_id: str) -> bool:
    """判断 (node_id, action_id) 是否处于去重窗口内（True=应跳过本次下发）。"""
    if _window_seconds() <= 0:
        return False
    key = _key(node_id, action_id)

    from app.core.task_queue import get_task_queue

    client = get_task_queue().get_redis_client()
    if client is not None:
        try:
            return bool(client.exists(key))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Redis 查询去重键失败，回退内存: {exc}")

    now = time.monotonic()
    with _mem_lock:
        exp = _mem_recent.get(key)
        return bool(exp and exp > now)


def record_dispatched(node_id: str, action_id: str) -> None:
    """记录一次自愈下发，设置去重窗口 TTL。"""
    window = _window_seconds()
    if window <= 0:
        return
    key = _key(node_id, action_id)

    from app.core.task_queue import get_task_queue

    client = get_task_queue().get_redis_client()
    if client is not None:
        try:
            client.set(key, "1", ex=window)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Redis 写入去重键失败，回退内存: {exc}")

    with _mem_lock:
        _mem_recent[key] = time.monotonic() + window


def reset() -> None:
    """清空进程内去重状态（测试隔离用）。"""
    with _mem_lock:
        _mem_recent.clear()
