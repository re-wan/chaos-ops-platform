"""Redis 任务队列封装（Phase 3 Step 05）。

设计目标（对齐 ``docs/core/PERFORMANCE_DESIGN.md`` §5）：

- 将指标写入、告警检测、通知发送等耗时/IO 操作从请求热路径中解耦，
  由后台 worker 异步消费，提升吞吐并平滑峰值。
- **Redis 不是硬依赖**：未配置 ``REDIS_URL`` 或 Redis 不可达时，自动降级为
  进程内同步处理（``enqueue`` 直接调用已注册 handler），Server 仍能启动工作，
  仅丢失异步加速能力（打 warning）。

可靠性语义：

- Redis 模式采用 ``BRPOPLPUSH`` 把任务从主队列弹入 ``processing`` 队列，
  处理成功后再 ``LREM`` 确认；启动时回收 ``processing`` 残留，保证 **at-least-once**，
  进程崩溃不丢任务（handler 需幂等，见各 worker 注释）。
- 失败重试：指数退避（``base * 2**(attempt-1)``）放入延迟重试有序集合，
  到期后重新推回主队列；超过 ``max_attempts`` 进入死信队列（DLQ）便于人工介入。
  重试/死信流转采用 **先写 retry/dlq 成功、再 ack processing** 的顺序，避免
  "ack 后写失败" 导致任务丢失（写成功但 ack 前崩溃产生的重复由消费方幂等吸收）。
- 运行期熔断：Redis 运行期操作失败时打开短窗熔断，窗口内 ``is_async()`` 返
  False，``enqueue`` 改走同步直调 handler（不重试、不 sleep、快速返回），窗口
  后首次 Redis 操作成功即恢复异步，避免瞬时抖动拖慢请求热路径。
- 背压：主队列达到 ``max_size`` 水位时 ``enqueue`` 抛 ``QueueFullError``，上游
  映射为 503 让 Agent 退避，防止 Redis OOM；DLQ 超 ``dlq_warn_size`` 记告警。
- 降级（fallback）模式：同步执行，仍按 ``max_attempts`` 重试，最终失败进入
  进程内 DLQ（仅用于观测/测试，重启即清空）。

本模块不直接依赖具体业务 handler；handler 在 ``app.workers`` 中注册，
启动入口见 ``app.workers.start_workers``（由 ``main.py`` lifespan 在 leader 上调用）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Optional

from app.core.logger import get_logger

logger = get_logger("core.task_queue")

# 队列 handler 类型：接收业务 payload（dict），无返回值；抛异常即视为处理失败。
Handler = Callable[[dict], None]

# Redis key 命名前缀，避免与其它业务 key 冲突。
_DEFAULT_PREFIX = "chaosops:tq"

# DLQ 水位告警的最小间隔（秒/队列），避免超阈值后刷屏。
_DLQ_WARN_INTERVAL_SECONDS = 60.0


class QueueFullError(Exception):
    """主队列达到水位阈值，拒绝入队。

    上游应将其映射为 503 并让 Agent/调用方退避重试，避免 Redis 内存无限增长。
    仅在异步（Redis）模式下触发；同步降级模式无队列，不触发。
    """


class TaskQueue:
    """轻量 Redis 队列，带重试 / 死信 / 降级。

    通过注入 ``redis_client`` 决定运行模式：

    - 传入可用的 Redis 客户端（含 ``redis.Redis`` 与 ``fakeredis.FakeRedis``）→
      分布式异步模式（``is_async() == True``），由后台线程消费。
    - ``redis_client is None`` → 进程内同步降级模式（``is_async() == False``），
      ``enqueue`` 直接调用 handler。
    """

    def __init__(
        self,
        redis_client: Optional[Any] = None,
        *,
        prefix: str = _DEFAULT_PREFIX,
        max_attempts: int = 5,
        backoff_base: float = 1.0,
        block_timeout: int = 2,
        concurrency: int = 2,
        circuit_ttl: float = 30.0,
        max_size: int = 100000,
        dlq_warn_size: int = 1000,
    ) -> None:
        self._redis = redis_client
        self._prefix = prefix.rstrip(":")
        self._max_attempts = max(1, int(max_attempts))
        self._backoff_base = max(0.0, float(backoff_base))
        self._block_timeout = max(1, int(block_timeout))
        self._concurrency = max(1, int(concurrency))
        self._circuit_ttl = max(0.0, float(circuit_ttl))
        self._max_size = max(0, int(max_size))
        self._dlq_warn_size = max(0, int(dlq_warn_size))

        self._handlers: dict[str, Handler] = {}
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._started = False

        # 运行期 Redis 故障熔断：当 wall-time 早于该时间戳时，is_async() 返 False，
        # enqueue 改走同步直调。0.0 表示未触发。
        self._circuit_open_until: float = 0.0
        # DLQ 水位告警节流（queue -> 上次告警时间戳）。
        self._dlq_warn_last: dict[str, float] = {}

        # 降级模式的进程内 DLQ（queue -> list[job]），仅供观测与测试。
        self._fallback_dlq: dict[str, list[dict]] = {}

        # 统计计数（线程安全通过 _lock 保护）。
        self._stats = {
            "enqueued": 0,
            "processed": 0,
            "retried": 0,
            "failed": 0,
            "dlq": 0,
        }

    # ------------------------------------------------------------------ #
    # 基础属性
    # ------------------------------------------------------------------ #
    @property
    def is_fallback(self) -> bool:
        """是否处于进程内同步降级模式（无 Redis）。"""
        return self._redis is None

    def is_async(self) -> bool:
        """是否真正异步（Redis 后端且未处于熔断窗口）。

        业务代码可用此判断是否在热路径上选择"入队即返回"的加速分支；
        降级模式或运行期 Redis 故障熔断窗口内应走同步路径以保证语义一致、
        不在请求线程内反复重试/sleep。
        """
        if self._redis is None:
            return False
        if self._circuit_open_until and time.time() < self._circuit_open_until:
            return False
        return True

    def get_redis_client(self) -> Optional[Any]:
        """返回底层 Redis 客户端（降级模式为 None）。

        供分布式限流计数、自愈去重键等复用同一连接，避免重复建连。
        """
        return self._redis

    @property
    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    # Redis key 构造
    def _main_key(self, queue: str) -> str:
        return f"{self._prefix}:{queue}"

    def _processing_key(self, queue: str) -> str:
        return f"{self._prefix}:{queue}:processing"

    def _retry_key(self, queue: str) -> str:
        return f"{self._prefix}:{queue}:retry"

    def _dlq_key(self, queue: str) -> str:
        return f"{self._prefix}:{queue}:dlq"

    # ------------------------------------------------------------------ #
    # Handler 注册
    # ------------------------------------------------------------------ #
    def register_handler(self, queue: str, handler: Handler) -> None:
        """为指定队列注册处理函数（重复注册以后者为准）。"""
        with self._lock:
            self._handlers[queue] = handler

    def get_handler(self, queue: str) -> Optional[Handler]:
        with self._lock:
            return self._handlers.get(queue)

    # ------------------------------------------------------------------ #
    # 入队
    # ------------------------------------------------------------------ #
    def enqueue(
        self,
        queue: str,
        payload: dict,
        *,
        max_attempts: Optional[int] = None,
    ) -> str:
        """入队一个任务，返回 job_id。

        - Redis 模式：序列化后 LPUSH 到主队列，由后台 worker 异步消费。
        - 降级模式：直接同步调用 handler（带重试），失败进入进程内 DLQ。
        """
        attempts_limit = max(1, int(max_attempts or self._max_attempts))
        job = {
            "id": uuid.uuid4().hex,
            "queue": queue,
            "payload": payload,
            "attempts": 0,
            "max_attempts": attempts_limit,
            "enqueued_at": time.time(),
        }
        with self._lock:
            self._stats["enqueued"] += 1

        if self.is_fallback:
            self._run_fallback(queue, job)
            return job["id"]

        # 运行期 Redis 故障熔断窗口内 → 同步直调 handler 一次，不重试、不 sleep，
        # 避免拖慢指标接收等请求热路径；handler 与异步投递相同（幂等一致）。
        if not self.is_async():
            self._run_sync_direct(queue, job)
            return job["id"]

        # 背压：主队列达到水位阈值则拒绝入队，交由上游返回 503 / 退避，防 Redis OOM。
        if self._is_over_capacity(queue):
            raise QueueFullError(
                f"队列 {queue} 已达到水位阈值 {self._max_size}，拒绝入队"
            )

        raw = json.dumps(job, ensure_ascii=False)
        try:
            self._redis.lpush(self._main_key(queue), raw)
            # 成功入队：若此前处于熔断恢复期，清理标记并记录恢复。
            self._maybe_clear_circuit()
        except Exception as exc:  # noqa: BLE001
            # Redis 运行期故障：触发熔断并本次降级同步直调，尽量不影响业务。
            logger.warning(
                f"Redis 入队失败，触发熔断并同步直调: queue={queue}, error={exc}"
            )
            self._trip_circuit()
            self._run_sync_direct(queue, job)
        return job["id"]

    # ------------------------------------------------------------------ #
    # Worker 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """启动后台 worker 线程（Redis 模式）；降级模式为 no-op。"""
        if self.is_fallback:
            logger.info("任务队列处于降级模式（无 Redis），不启动后台 worker")
            return
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop_event.clear()
            queues = list(self._handlers.keys())

        # 启动前回收上一次崩溃残留在 processing 队列的任务，避免丢失。
        for queue in queues:
            self._recover_processing(queue)

        for queue in queues:
            for i in range(self._concurrency):
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(queue,),
                    name=f"tq-{queue}-{i}",
                    daemon=True,
                )
                t.start()
                self._threads.append(t)
        logger.info(
            f"任务队列 worker 已启动: queues={queues}, concurrency={self._concurrency}"
        )

    def stop(self, timeout: float = 10.0) -> None:
        """优雅停止 worker，等待进行中的任务完成（最长 ``timeout`` 秒）。"""
        self._stop_event.set()
        threads: list[threading.Thread]
        with self._lock:
            threads = list(self._threads)
            self._threads = []
            self._started = False
        deadline = time.time() + timeout
        for t in threads:
            remaining = max(0.0, deadline - time.time())
            t.join(timeout=remaining)
        logger.info("任务队列 worker 已停止")

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #
    def size(self, queue: str) -> int:
        """主队列中等待处理的任务数（降级模式恒为 0）。"""
        if self.is_fallback:
            return 0
        try:
            return int(self._redis.llen(self._main_key(queue)) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def dlq_size(self, queue: str) -> int:
        """死信队列任务数（降级模式返回进程内 DLQ 长度）。"""
        if self.is_fallback:
            with self._lock:
                return len(self._fallback_dlq.get(queue, []))
        try:
            return int(self._redis.llen(self._dlq_key(queue)) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def retry_size(self, queue: str) -> int:
        """等待重试的任务数（降级模式恒为 0）。"""
        if self.is_fallback:
            return 0
        try:
            return int(self._redis.zcard(self._retry_key(queue)) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def fallback_dlq(self, queue: str) -> list[dict]:
        """降级模式进程内 DLQ 快照（仅测试/观测用）。"""
        with self._lock:
            return list(self._fallback_dlq.get(queue, []))

    # ------------------------------------------------------------------ #
    # 内部：运行期熔断与背压
    # ------------------------------------------------------------------ #
    def _trip_circuit(self) -> None:
        """运行期 Redis 失败时打开熔断：窗口内 ``is_async()`` 返 False。

        仅在当前未处于熔断窗口时刷新截止时间并记 warning，避免窗口被持续失败
        无限延长而无法探测恢复，也避免日志刷屏。
        """
        now = time.time()
        if now >= self._circuit_open_until:
            self._circuit_open_until = now + self._circuit_ttl
            logger.warning(
                f"任务队列触发熔断，{self._circuit_ttl:.0f}s 内降级同步直调"
            )

    def _maybe_clear_circuit(self) -> None:
        """Redis 操作成功后，若熔断窗口已结束则清理标记并记录恢复。"""
        if self._circuit_open_until and time.time() >= self._circuit_open_until:
            self._circuit_open_until = 0.0
            logger.info("任务队列熔断已恢复，回到异步模式")

    def _is_over_capacity(self, queue: str) -> bool:
        """主队列是否达到水位阈值（背压）。观测失败不误判为满。"""
        if self._max_size <= 0:
            return False
        try:
            current = int(self._redis.llen(self._main_key(queue)) or 0)
        except Exception:  # noqa: BLE001
            # 观测失败时不误判为满；若 Redis 真故障，后续 lpush 会触发熔断。
            return False
        return current >= self._max_size

    # ------------------------------------------------------------------ #
    # 内部：worker 主循环
    # ------------------------------------------------------------------ #
    def _worker_loop(self, queue: str) -> None:
        handler = self.get_handler(queue)
        if handler is None:
            logger.warning(f"队列 {queue} 无 handler，worker 退出")
            return
        main = self._main_key(queue)
        processing = self._processing_key(queue)

        while not self._stop_event.is_set():
            try:
                self._promote_due_retries(queue)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"提升到期重试任务失败: queue={queue}, error={exc}")

            try:
                # 阻塞式从主队列右端弹出并压入 processing（原子），保证崩溃可恢复。
                raw = self._redis.brpoplpush(
                    main, processing, timeout=self._block_timeout
                )
            except Exception as exc:  # noqa: BLE001
                # 连接抖动：短暂退避后继续，避免空转打爆日志。
                logger.warning(f"Redis 取任务失败: queue={queue}, error={exc}")
                if self._stop_event.wait(1.0):
                    break
                continue

            if raw is None:
                # 超时无任务，回到循环检查 stop。
                continue
            self._handle_raw(queue, handler, raw)

    def _handle_raw(self, queue: str, handler: Handler, raw: Any) -> None:
        """处理一个原始任务（已位于 processing 队列）。"""
        processing = self._processing_key(queue)
        try:
            job = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            # 无法解析的脏数据：直接从 processing 移除并丢弃，避免阻塞队列。
            logger.warning(f"任务反序列化失败，丢弃: queue={queue}, error={exc}")
            self._lrem(processing, raw)
            return

        payload = job.get("payload", {})
        try:
            handler(payload)
        except Exception as exc:  # noqa: BLE001
            self._on_failure(queue, job, raw, exc)
            return
        self._ack(processing, raw)
        with self._lock:
            self._stats["processed"] += 1

    def _on_failure(self, queue: str, job: dict, raw: Any, exc: Exception) -> None:
        """处理失败：先落 retry/dlq，成功后再确认 processing（at-least-once）。

        顺序保证：先 ``zadd retry`` / ``lpush dlq`` 成功，再 ``lrem processing`` 确认。
        若写入 retry/dlq 失败则不确认，任务留在 processing 待启动回收重放，
        保证不丢；若写入成功但确认前崩溃，可能产生重复投递，由消费方幂等吸收
        （metric.write 同键覆盖、alert.detect 状态机去重、notify.send log_id 幂等）。
        """
        processing = self._processing_key(queue)
        attempts = int(job.get("attempts", 0)) + 1
        job["attempts"] = attempts
        max_attempts = int(job.get("max_attempts", self._max_attempts))
        job["last_error"] = str(exc)[:500]

        if attempts >= max_attempts:
            if self._push_dlq(queue, job):
                self._ack(processing, raw)
                with self._lock:
                    self._stats["failed"] += 1
                    self._stats["dlq"] += 1
                logger.warning(
                    f"任务进入死信队列: queue={queue}, job_id={job.get('id')}, "
                    f"attempts={attempts}, error={exc}"
                )
            # 写 DLQ 失败：不确认，留在 processing 待回收重放，保证不丢。
            return

        # 指数退避后进入延迟重试有序集合。
        delay = self._backoff_base * (2 ** (attempts - 1))
        next_at = time.time() + delay
        new_raw = json.dumps(job, ensure_ascii=False)
        if self._push_retry(queue, new_raw, next_at):
            self._ack(processing, raw)
            with self._lock:
                self._stats["retried"] += 1
            logger.info(
                f"任务将重试: queue={queue}, job_id={job.get('id')}, "
                f"attempt={attempts}/{max_attempts}, delay={delay:.2f}s, error={exc}"
            )
        # 写 retry 失败：不确认，留在 processing 待回收重放，保证不丢。

    def _push_retry(self, queue: str, new_raw: str, score: float) -> bool:
        """写入延迟重试有序集合，返回是否成功。失败时由调用方保留 processing。"""
        try:
            self._redis.zadd(self._retry_key(queue), {new_raw: score})
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"写入重试队列失败（任务保留在 processing 待回收）: "
                f"queue={queue}, error={exc}"
            )
            return False

    def _push_dlq(self, queue: str, job: dict) -> bool:
        """写入死信队列并做水位告警，返回是否成功。失败时由调用方保留 processing。"""
        try:
            self._redis.lpush(
                self._dlq_key(queue), json.dumps(job, ensure_ascii=False)
            )
            self._warn_dlq_if_needed(queue)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"写入 DLQ 失败（任务保留在 processing 待回收）: "
                f"queue={queue}, error={exc}"
            )
            return False

    def _warn_dlq_if_needed(self, queue: str) -> None:
        """DLQ 超阈值时节流记 warning（不阻塞，仅观测）。"""
        if self._dlq_warn_size <= 0:
            return
        try:
            size = int(self._redis.llen(self._dlq_key(queue)) or 0)
        except Exception:  # noqa: BLE001
            return
        if size < self._dlq_warn_size:
            return
        now = time.time()
        with self._lock:
            last = self._dlq_warn_last.get(queue, 0.0)
            if now - last < _DLQ_WARN_INTERVAL_SECONDS:
                return
            self._dlq_warn_last[queue] = now
        logger.warning(
            f"DLQ 水位告警: queue={queue}, size={size}, "
            f"threshold={self._dlq_warn_size}"
        )

    def _promote_due_retries(self, queue: str) -> None:
        """将到期的重试任务从有序集合推回主队列。"""
        retry = self._retry_key(queue)
        now = time.time()
        # 取出所有到期的任务（按 score <= now）。
        due = self._redis.zrangebyscore(retry, "-inf", now)
        if not due:
            return
        for raw in due:
            # 先删后推，删除失败则跳过避免重复投递。
            removed = self._redis.zrem(retry, raw)
            if removed:
                self._redis.lpush(self._main_key(queue), raw)

    def _recover_processing(self, queue: str) -> None:
        """启动时回收 processing 残留任务，推回主队列（at-least-once）。"""
        processing = self._processing_key(queue)
        main = self._main_key(queue)
        try:
            while True:
                raw = self._redis.rpoplpush(processing, main)
                if raw is None:
                    break
                with self._lock:
                    self._stats["retried"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"回收 processing 队列失败: queue={queue}, error={exc}")

    @staticmethod
    def _lrem(client_key: str, raw: Any) -> None:
        """从 processing 队列确认移除一个任务（容错）。"""
        # 注：self 绑定在调用方，这里通过闭包使用 self._redis。
        raise AssertionError("不应直接调用，请使用实例方法 _ack")

    def _ack(self, processing: str, raw: Any) -> None:
        try:
            self._redis.lrem(processing, 1, raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"确认任务失败(LREM): error={exc}")

    # 用 _ack 替代上面误定义的静态 _lrem 调用
    def _lrem(self, processing: str, raw: Any) -> None:  # type: ignore[no-redef]
        self._ack(processing, raw)

    # ------------------------------------------------------------------ #
    # 内部：降级模式同步执行
    # ------------------------------------------------------------------ #
    def _run_sync_direct(self, queue: str, job: dict) -> None:
        """运行期熔断降级：同步直接调用 handler 一次，不重试、不 sleep、快速返回。

        与异步"单次投递"语义一致（同一 handler、同一幂等）；失败进入进程内 DLQ
        供观测，不阻塞请求线程。用于 Redis 运行期故障/熔断窗口内的热路径调用。
        """
        handler = self.get_handler(queue)
        if handler is None:
            logger.warning(
                f"同步直调队列 {queue} 无 handler，任务进入 DLQ: {job.get('id')}"
            )
            self._fallback_to_dlq(queue, job, "no handler registered")
            return
        try:
            handler(job.get("payload", {}))
            with self._lock:
                self._stats["processed"] += 1
        except Exception as exc:  # noqa: BLE001
            # 不重试、不 sleep：避免拖慢请求线程；落入进程内 DLQ 保证不丢、可观测。
            logger.warning(
                f"同步直调失败，进入 DLQ: queue={queue}, "
                f"job_id={job.get('id')}, error={exc}"
            )
            self._fallback_to_dlq(queue, job, str(exc)[:500])

    def _run_fallback(self, queue: str, job: dict) -> None:
        """降级模式：同步执行 handler，按 max_attempts 重试，失败进入进程内 DLQ。"""
        handler = self.get_handler(queue)
        if handler is None:
            logger.warning(f"降级模式队列 {queue} 无 handler，任务丢弃: {job.get('id')}")
            self._fallback_to_dlq(queue, job, "no handler registered")
            return

        payload = job.get("payload", {})
        max_attempts = int(job.get("max_attempts", self._max_attempts))
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                handler(payload)
                with self._lock:
                    self._stats["processed"] += 1
                return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                job["attempts"] = attempt
                if attempt < max_attempts:
                    delay = self._backoff_base * (2 ** (attempt - 1))
                    # 降级模式下退避做上限裁剪，避免阻塞请求线程过久。
                    time.sleep(min(delay, 1.0))
                    with self._lock:
                        self._stats["retried"] += 1
        self._fallback_to_dlq(queue, job, str(last_exc)[:500] if last_exc else "failed")

    def _fallback_to_dlq(self, queue: str, job: dict, error: str) -> None:
        job["last_error"] = error
        with self._lock:
            self._fallback_dlq.setdefault(queue, []).append(job)
            self._stats["failed"] += 1
            self._stats["dlq"] += 1
        logger.warning(
            f"降级模式任务进入进程内 DLQ: queue={queue}, job_id={job.get('id')}, "
            f"error={error}"
        )


# ---------------------------------------------------------------------- #
# 全局单例与配置解析
# ---------------------------------------------------------------------- #

_task_queue: Optional[TaskQueue] = None
_singleton_lock = threading.Lock()


def _redis_available(redis_client: Any) -> bool:
    """探测 Redis 是否可达。"""
    try:
        return bool(redis_client.ping())
    except Exception:  # noqa: BLE001
        return False


def _build_from_settings() -> TaskQueue:
    """根据 settings 构造 TaskQueue（含降级决策）。"""
    from app.core.config import settings

    mode = (settings.TASK_QUEUE_ENABLED or "auto").lower().strip()
    redis_url = (settings.REDIS_URL or "").strip()

    want_redis = mode == "on" or (mode == "auto" and bool(redis_url))
    if mode == "off":
        want_redis = False

    redis_client = None
    if want_redis and redis_url:
        try:
            import redis as redis_lib  # 延迟导入，未安装也不影响启动

            candidate = redis_lib.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            if _redis_available(candidate):
                redis_client = candidate
                logger.info(f"任务队列已连接 Redis: {_safe_url(redis_url)}")
            else:
                logger.warning("Redis 不可达（ping 失败），任务队列降级为同步处理")
        except ImportError:
            logger.warning("未安装 redis 包，任务队列降级为同步处理")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"连接 Redis 失败，任务队列降级为同步处理: {exc}")
    elif want_redis and not redis_url:
        logger.warning("TASK_QUEUE_ENABLED=on 但未配置 REDIS_URL，降级为同步处理")

    return TaskQueue(
        redis_client=redis_client,
        max_attempts=settings.TASK_QUEUE_MAX_ATTEMPTS,
        backoff_base=settings.TASK_QUEUE_BACKOFF_BASE_SECONDS,
        block_timeout=settings.TASK_QUEUE_BLOCK_TIMEOUT_SECONDS,
        concurrency=settings.TASK_QUEUE_WORKER_CONCURRENCY,
        circuit_ttl=settings.TASK_QUEUE_CIRCUIT_TTL_SECONDS,
        max_size=settings.TASK_QUEUE_MAX_SIZE,
        dlq_warn_size=settings.TASK_QUEUE_DLQ_WARN_SIZE,
    )


def _safe_url(url: str) -> str:
    """脱敏 Redis URL（隐藏密码）用于日志。"""
    if "@" not in url:
        return url
    try:
        scheme, rest = url.split("://", 1)
        creds_host = rest.split("@", 1)
        if ":" in creds_host[0]:
            user = creds_host[0].split(":", 1)[0]
            return f"{scheme}://{user}:***@{creds_host[1]}"
    except Exception:  # noqa: BLE001
        pass
    return "redis://***"


def get_task_queue() -> TaskQueue:
    """获取全局 TaskQueue 单例（按配置懒初始化）。"""
    global _task_queue
    if _task_queue is None:
        with _singleton_lock:
            if _task_queue is None:
                _task_queue = _build_from_settings()
    return _task_queue


def set_task_queue(queue: Optional[TaskQueue]) -> None:
    """注入/替换全局 TaskQueue（主要供测试注入 fakeredis 后端）。"""
    global _task_queue
    with _singleton_lock:
        _task_queue = queue


def reset_task_queue() -> None:
    """停止并重置全局 TaskQueue（测试隔离用）。"""
    global _task_queue
    with _singleton_lock:
        old = _task_queue
        _task_queue = None
    if old is not None:
        try:
            old.stop(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
