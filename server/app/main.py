"""ChaosOps Server FastAPI 入口。"""

import asyncio
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from sqlmodel import Session

# 导入模型，确保 SQLModel.metadata 在 init_db 调用前注册所有表
from app import models  # noqa: F401
# 付费模块防御式 import：三版物理分包会删除这些文件；
# 文件存在时（全量/dev/企业版）行为与原来完全一致。
try:
    from app.api.ai_heal import router as ai_heal_router
    HAS_AI_HEAL = True
except ImportError:
    ai_heal_router = None
    HAS_AI_HEAL = False

try:
    from app.api.ai_tools import router as ai_tools_router
    HAS_AI_TOOLS = True
except ImportError:
    ai_tools_router = None
    HAS_AI_TOOLS = False

from app.api.admin import router as admin_router
from app.api.admin_metric_backend import router as admin_metric_backend_router
from app.api.agents import router as agents_router
from app.api.users import router as users_router
from app.api.alert_inhibitions import router as alert_inhibitions_router
from app.api.alert_rules import router as alert_rules_router
from app.api.alert_silences import router as alert_silences_router
from app.api.alerts import router as alerts_router
from app.api.auth import router as auth_router
from app.api.dashboard import router as dashboard_router
from app.api.heal_actions import router as heal_actions_router
from app.api.heal_rules import router as heal_rules_router
from app.api.heal_tasks import router as heal_tasks_router
from app.api.incidents import router as incidents_router
from app.api.agent_version import admin_router as agent_version_admin_router
from app.api.agent_version import router as agent_version_router
try:
    from app.api.api_keys import router as api_keys_admin_router
    HAS_API_KEYS = True
except ImportError:
    api_keys_admin_router = None
    HAS_API_KEYS = False

try:
    from app.api.open_api import router as open_api_router
    HAS_OPEN_API = True
except ImportError:
    open_api_router = None
    HAS_OPEN_API = False

try:
    from app.api.webhooks import router as webhooks_router
    HAS_WEBHOOKS = True
except ImportError:
    webhooks_router = None
    HAS_WEBHOOKS = False

from app.api.license import router as license_router
from app.api.license_claim import router as license_claim_router

from app.api.agent_tasks import router as agent_tasks_router
from app.api.backup import router as backup_router
from app.api.metrics import router as metrics_router
from app.api.node_groups import router as node_groups_router
try:
    from app.api.remote_execution import router as remote_execution_router
    HAS_REMOTE_EXECUTION = True
except ImportError:
    remote_execution_router = None
    HAS_REMOTE_EXECUTION = False

from app.api.nodes import router as nodes_router
from app.api.notification_channels import router as notification_channels_router
from app.api.notification_templates import router as notification_templates_router
try:
    from app.api.optimizations import router as optimizations_router
    HAS_OPTIMIZATIONS = True
except ImportError:
    optimizations_router = None
    HAS_OPTIMIZATIONS = False

from app.api.websocket import router as websocket_router
from app.services.alert_detector import init_alert_detector, stop_alert_detector
from app.services.heal_action import init_builtin_actions
from app.services.metrics_ingest import close_metric_backend, get_metric_backend
from app.core.system_settings import get_metric_backend_type
from app.core.config import settings
from app.core import database as database_module
from app.core.database import engine, init_db
from app.core import licensing
from app.core.logger import get_logger, setup_logging
from app.core.resilience import StartupCheckError, ensure_database_integrity, run_startup_checks
from app.core.scheduler import shutdown_scheduler, start_scheduler
from app.core.single_instance import SingleInstanceGuard
from app.services.node_service import ensure_local_node, touch_node_online
from app.services import audit_log

logger = get_logger("main")

# 后台任务单实例守卫：仅 leader worker 运行调度器/告警检测/本地 Agent。
# 需在进程生命周期内持有，故放在模块级；进程退出时锁自动释放。
_background_guard: SingleInstanceGuard | None = None
# 领导权 watchdog 任务（多 worker leader 崩溃自动接管，收尾修复第 9 批）。
_watchdog_task: "asyncio.Task[None] | None" = None
# 后台任务幂等标志：保证 _start_background_tasks 同进程内只生效一次。
_background_started: bool = False
# 本地 Agent 子进程句柄提至模块级：watchdog 接管时同样要拉起它，shutdown 统一回收。
_local_agent_process: "subprocess.Popen[bytes] | None" = None
_local_agent_log_handle = None
# 本地 Agent 死亡自动重启状态（批 16，H2 修复）：
# - _local_agent_started_at：最近一次拉起 Agent 的单调时钟时刻，用于判定“稳定存活”。
# - _local_agent_dead_at：发现 Agent 死亡的单调时钟时刻（None=无待处理死亡），冷却计时起点。
# - _local_agent_restart_times：滑动窗口内的重启时刻列表，用于崩溃循环上限判定。
# - _local_agent_gave_up：达上限后置 True，停止自动重启直至进程重启（人工介入）。
_local_agent_started_at: float | None = None
_local_agent_dead_at: float | None = None
_local_agent_restart_times: list[float] = []
_local_agent_gave_up: bool = False


def _start_background_tasks() -> None:
    """启动 leader 专属的全部后台任务（调度器 / 告警检测 / 队列 worker / 本地 Agent）。

    后台任务启动的**唯一入口**：lifespan 首次取锁成功、以及领导权 watchdog 接管时
    都走这里，不允许另开路径。幂等：`_background_started` 保证重复调用不产生第二份
    任务；各子启动函数也自带幂等保护（scheduler.running / _detector / queue._started），
    本地 Agent 启动放在最后且自身吞异常，故标志位置于末尾——中途异常时 watchdog
    下一 tick 可安全整体重试，不会重复拉起任何组件。
    """
    global _background_started, _local_agent_process, _local_agent_log_handle
    global _local_agent_started_at
    if _background_started:
        return

    # 启动后台任务调度器
    start_scheduler()

    # 启动告警检测引擎
    if settings.ALERT_DETECTOR_ENABLED:
        init_alert_detector(database_module.engine)

    # Phase 3 Step 05：启动 Redis 任务队列 worker（降级模式为 no-op）。
    # 仅 leader 消费，保证告警检测/指标落库与单实例守卫一致，不重复处理。
    from app.workers import start_workers

    start_workers()

    # 启动本地 Agent 子进程（同样受单实例守卫约束）
    if settings.LOCAL_AGENT_ENABLED:
        try:
            with Session(database_module.engine) as session:
                agent_token = ensure_local_node(session).agent_token
            _local_agent_process, _local_agent_log_handle = start_local_agent(agent_token)
            if _local_agent_process is not None:
                _local_agent_started_at = time.monotonic()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动本地 Agent 失败（不影响其他后台任务）: {e}")

    _background_started = True


def _resolve_agent_pythonpath() -> str | None:
    """探测 agent 包的父目录，供本地 Agent 子进程注入 PYTHONPATH。

    ``python -m agent.app.local_main`` 要求 agent 包的父目录在 sys.path 上。
    生产环境 systemd WorkingDirectory 即该目录（cwd 解析 OK）；开发环境
    cwd=backend/ 而 agent/ 在项目根，cwd 解析失败导致子进程秒崩
    （ModuleNotFoundError）。故按以下优先级探测第一个含 ``agent/__init__.py``
    的目录：当前 cwd → cwd 父目录 → 本文件各级祖先目录。

    找不到时返回 None（调用方记录 warning 并仍按原方式启动），不 fail-closed
    误伤依赖 cwd 解析即可工作的既有部署。
    """
    cwd = Path.cwd()
    candidates = [cwd, cwd.parent, *Path(__file__).resolve().parents]
    seen: set[Path] = set()
    for base in candidates:
        if base in seen:
            continue
        seen.add(base)
        if (base / "agent" / "__init__.py").is_file():
            return str(base)
    return None


def _reap_local_agent() -> None:
    """回收已退出的本地 Agent 子进程（防 zombie）、告警，并按策略自动重启（批 16，H2）。

    本地 Agent 是 server 的 Popen 子进程：若无人 wait，退出后变 zombie；且
    server 对其死亡无感知（反向监控静默缺失）。本函数由领导权 watchdog
    每 tick 调用（poll 非阻塞，不阻塞主循环）：发现已退出则 wait 回收、
    按退出码记 error/warning、置空模块句柄（避免重复告警，且与 shutdown
    路径``_local_agent_process is not None`` 判空天然兼容，不重复 terminate）。

    **自动重启**（批 16 Owner 拍板）：死亡告警后进入冷却
    （``LOCAL_AGENT_RESTART_COOLDOWN_SECONDS``），冷却到期由本函数重新拉起；
    滑动窗口（``LOCAL_AGENT_RESTART_WINDOW_SECONDS``）内重启达
    ``LOCAL_AGENT_RESTART_MAX_FAILURES`` 次则放弃并 error 告警人工介入
    （崩溃循环防护）；Agent 稳定存活超冷却期后重启计数重置（衰减）。
    仅在三个条件同时满足时重启：本进程是 leader（guard.acquired）、
    ``LOCAL_AGENT_ENABLED``、未放弃。shutdown 路径句柄已置 None 且 watchdog
    先停，不触发。
    """
    global _local_agent_process, _local_agent_log_handle, _local_agent_dead_at
    if _local_agent_process is None:
        # 无存活句柄：处理冷却到期后的待重启（无待处理死亡则空转）
        _maybe_restart_local_agent()
        return
    returncode = _local_agent_process.poll()
    if returncode is None:
        # Agent 存活：顺手维持 __local__ 节点在线（任务 5 发现本地节点
        # 无人上报 last_seen，会被离线标记任务误判 offline）。
        _touch_local_node_online()
        return  # 仍在运行
    # poll 已确认退出，wait 立即返回，仅做 zombie 回收
    _local_agent_process.wait()
    if returncode == 0:
        logger.warning("本地 Agent 已退出（exit code=0），反向监控缺失，将按策略自动重启")
    else:
        logger.error(
            f"本地 Agent 异常退出（exit code={returncode}），反向监控缺失，将按策略自动重启；"
            "退出详情见 logs/local_agent.log"
        )
    _local_agent_process = None
    # 日志句柄配对关闭：子进程已退出，fd 数据已落盘，此时关闭无丢失
    if _local_agent_log_handle is not None:
        _local_agent_log_handle.close()
        _local_agent_log_handle = None
    # 稳定存活超冷却期后死亡 → 重启计数重置（衰减：偶发死亡不计入崩溃循环）
    now = time.monotonic()
    if (
        _local_agent_started_at is not None
        and now - _local_agent_started_at >= settings.LOCAL_AGENT_RESTART_COOLDOWN_SECONDS
    ):
        _local_agent_restart_times.clear()
    # 记录死亡时刻作为冷却起点，随后尝试重启（冷却期内则下轮 tick 再试）
    _local_agent_dead_at = now
    _maybe_restart_local_agent()


_local_node_touched_at: float = 0.0
_LOCAL_NODE_TOUCH_INTERVAL_SECONDS = 30.0  # 远小于离线判定阈值（300s）


def _touch_local_node_online() -> None:
    """本地 Agent 存活期间维持 ``__local__`` 节点在线状态（节流 30s）。

    任务 5 Docker 端到端验证发现：本地 Agent 只执行 server 下发的本地任务，
    不上报指标也不心跳，``last_seen`` 无人更新 → ``check_and_mark_offline_nodes``
    （阈值 300s）会把本地节点误判为 offline，仪表盘误报。由领导权 watchdog
    每 tick（5s）调用，内部 30s 节流，每次仅一次轻量 commit。
    失败只告警：节点状态是展示层问题，不能影响 Agent 生命周期管理。
    """
    global _local_node_touched_at
    now_mono = time.monotonic()
    if now_mono - _local_node_touched_at < _LOCAL_NODE_TOUCH_INTERVAL_SECONDS:
        return
    _local_node_touched_at = now_mono
    try:
        with Session(engine) as session:
            node = ensure_local_node(session)
            touch_node_online(session, node)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"更新本地节点在线状态失败: {exc}")


def _maybe_restart_local_agent() -> None:
    """冷却到期且未达上限时重新拉起本地 Agent（由 _reap_local_agent 每 tick 调用）。

    重启条件（全部满足才拉起）：有待处理死亡（_local_agent_dead_at 非 None）、
    无存活句柄、LOCAL_AGENT_ENABLED、本进程为 leader、未放弃、冷却到期、
    滑动窗口内重启次数未达上限。单调时钟计时，不受系统时间回拨影响。
    """
    global _local_agent_process, _local_agent_log_handle, _local_agent_dead_at
    global _local_agent_started_at, _local_agent_gave_up
    if _local_agent_dead_at is None or _local_agent_process is not None:
        return
    if not settings.LOCAL_AGENT_ENABLED:
        return
    # 仅 leader 重启：非 leader worker 永不持有 Agent 句柄，shutdown 时 watchdog
    # 已先停，均不会走到这里；guard 为 None（未初始化，如纯单测）同样不重启。
    guard = _background_guard
    if guard is None or not guard.acquired:
        return
    if _local_agent_gave_up:
        return
    now = time.monotonic()
    if now - _local_agent_dead_at < settings.LOCAL_AGENT_RESTART_COOLDOWN_SECONDS:
        return  # 冷却期内，下轮 tick 再试
    # 滑动窗口上限：窗口内重启达上限 → 放弃自动重启，告警人工介入
    window = settings.LOCAL_AGENT_RESTART_WINDOW_SECONDS
    _local_agent_restart_times[:] = [t for t in _local_agent_restart_times if now - t < window]
    if len(_local_agent_restart_times) >= settings.LOCAL_AGENT_RESTART_MAX_FAILURES:
        _local_agent_gave_up = True
        logger.error(
            f"本地 Agent 反复崩溃（{int(window)} 秒内重启 {len(_local_agent_restart_times)} 次），"
            "已放弃自动重启，请人工介入"
        )
        return
    # 拉起（复用启动函数，日志句柄配对；失败则进入下一轮冷却重试）
    try:
        with Session(database_module.engine) as session:
            agent_token = ensure_local_node(session).agent_token
        process, log_handle = start_local_agent(agent_token)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"自动重启本地 Agent 失败（下轮冷却后再试）: {e}")
        _local_agent_restart_times.append(now)
        _local_agent_dead_at = now
        return
    if process is None:
        # start_local_agent 内部已记 error；同样计入重启次数并进入下轮冷却
        _local_agent_restart_times.append(now)
        _local_agent_dead_at = now
        return
    _local_agent_process = process
    _local_agent_log_handle = log_handle
    _local_agent_started_at = now
    _local_agent_dead_at = None
    _local_agent_restart_times.append(now)
    logger.info(f"本地 Agent 已自动重启，PID: {process.pid}")


async def _leadership_watchdog(guard: SingleInstanceGuard) -> None:
    """领导权 watchdog：未持锁时定时非阻塞重试取锁，leader 崩溃后自动接管后台任务。

    多 worker 部署下 acquire() 只在启动调一次——leader 崩溃后锁虽被 OS 回收，其余 worker
    却不会再来取锁，造成"HTTP 正常、后台全停"的静默停摆。本任务每隔
    ``SINGLE_INSTANCE_WATCHDOG_SECONDS`` 秒重试一次，抢到锁即接管（与产品"崩溃后无需
    人工干预自动恢复"的卖点一致）。

    - 已持锁（含无 fcntl 的 fail-safe 降级）→ ``not guard.acquired`` 短路空转；
    - 显式标记的非 leader worker（SERVER_WORKER_ID）→ acquire() 恒 False，永久空转；
    - sleep 在 try 外：业务异常不影响下一轮调度，shutdown 取消时干净退出；
    - 业务段整体 try/except：任何异常只记日志，绝不允许 watchdog 杀死 worker；
    - 每 tick 顺带回收已退出的本地 Agent（防 zombie + 死亡告警，不自动重启）。
    """
    interval = max(0.01, float(settings.SINGLE_INSTANCE_WATCHDOG_SECONDS))
    while True:
        await asyncio.sleep(interval)
        try:
            _reap_local_agent()
            if not guard.acquired and guard.acquire():
                logger.info(
                    f"原 leader 已释放领导权，本 worker 接管后台任务 (pid={os.getpid()})"
                )
                _start_background_tasks()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"领导权 watchdog 本轮异常（已忽略，不影响 worker）: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。

    启动时执行自检、数据库完整性检查、创建本地节点、启动调度器和本地 Agent。
    """
    # 配置日志
    setup_logging()

    # 加载 License（不阻塞启动；失败时降级为免费版）。
    # 测试环境可能已提前设置 _cached_license，避免覆盖。
    try:
        if licensing._cached_license is None:
            licensing.refresh_license()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"加载 License 时出错: {e}")

    # 启动自检
    try:
        run_startup_checks()
    except StartupCheckError as e:
        logger.error(f"启动自检失败: {e}")
        sys.exit(1)

    # 数据库初始化
    init_db()

    # 数据库完整性检查（损坏时尝试从备份恢复）
    try:
        ensure_database_integrity()
    except StartupCheckError as e:
        logger.error(f"数据库完整性检查/恢复失败: {e}")
        sys.exit(1)

    # 回收超时的 running 更新任务（进程崩溃/重启残留），释放占用；不阻塞启动
    try:
        from app.api.agent_version import reclaim_stale_running_tasks

        with Session(database_module.engine) as session:
            reclaim_stale_running_tasks(session)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"回收超时更新任务失败（不阻塞启动）: {e}")

    # 确保本地默认节点存在
    # 使用 database_module.engine 以便测试环境可通过替换该属性注入测试数据库
    with Session(database_module.engine) as session:
        ensure_local_node(session)

    # 后台任务单实例守卫：多 worker 时只一份运行调度器/告警检测/本地 Agent，
    # 避免备份/检测/通知重试等重复执行。未获取领导权的 worker 仅处理 HTTP。
    global _background_guard, _watchdog_task, _background_started
    global _local_agent_process, _local_agent_log_handle
    _background_guard = SingleInstanceGuard("server_background")
    is_leader = _background_guard.acquire()

    if is_leader:
        _start_background_tasks()
    else:
        logger.info("未获取后台任务领导权，本 worker 仅处理 HTTP（跳过调度器/告警检测）")

    # 领导权 watchdog：无论是否 leader 都启动。leader 进程崩溃后锁被 OS 回收，
    # 其余 worker 在下一个 tick 自动接管后台任务，无需人工重启（收尾修复第 9 批）。
    _watchdog_task = asyncio.create_task(_leadership_watchdog(_background_guard))

    # 初始化预置自愈动作
    with Session(database_module.engine) as session:
        init_builtin_actions(session)

    # 初始化默认通知模板
    from app.core.default_templates import init_default_templates

    with Session(database_module.engine) as session:
        init_default_templates(session)

    # 初始化指标存储后端（按当前配置：influxdb / sqlite），不阻塞启动
    current_metric_backend = get_metric_backend_type()
    try:
        get_metric_backend()
        logger.info(f"指标存储后端已初始化: {current_metric_backend}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"指标存储后端初始化失败，指标写入将不可用: {e}")

    # 启动开放 API Webhook 分发器（订阅内部事件总线，零侵入业务发码点）
    if settings.OPEN_API_ENABLED and HAS_WEBHOOKS:
        from app.services.webhook import start_webhook_dispatcher

        start_webhook_dispatcher()

    logger.info("ChaosOps Server 启动完成")
    yield

    # 关闭阶段
    from app.services.realtime import stop_realtime_service

    stop_webhook_dispatcher = None
    if HAS_WEBHOOKS:
        try:
            from app.services.webhook import stop_webhook_dispatcher as _stop_webhook_dispatcher

            stop_webhook_dispatcher = _stop_webhook_dispatcher
        except ImportError:
            pass

    # 先停领导权 watchdog：避免停机过程中再次取锁触发接管
    if _watchdog_task is not None:
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except asyncio.CancelledError:
            pass
        _watchdog_task = None

    if stop_webhook_dispatcher is not None:
        stop_webhook_dispatcher()
    # Phase 3 Step 05：先停止任务队列 worker（等待进行中任务完成），再关检测器/调度器。
    from app.workers import stop_workers

    stop_workers()
    stop_alert_detector()
    shutdown_scheduler()
    # 释放单实例领导权（进程退出也会自动释放，这里显式释放以便同进程重启）
    if _background_guard is not None:
        _background_guard.release()
    # 重置幂等标志，支持同进程重启（测试 / 热重载场景）
    _background_started = False
    await stop_realtime_service()

    # 释放指标存储后端连接
    try:
        close_metric_backend()
        logger.info("指标存储后端连接已释放")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"关闭指标存储后端失败: {e}")
    if _local_agent_process is not None:
        try:
            _local_agent_process.terminate()
            _local_agent_process.wait(timeout=5)
        except Exception as e:
            logger.warning(f"关闭本地 Agent 时出错: {e}")
        finally:
            # 子进程退出后关闭日志句柄（fd 已由子进程 dup，此时关闭无数据丢失）
            if _local_agent_log_handle is not None:
                _local_agent_log_handle.close()
                _local_agent_log_handle = None
            _local_agent_process = None
    logger.info("ChaosOps Server 已关闭")


def start_local_agent(agent_token: str):
    """启动本地 Agent 子进程。

    本地 Agent 使用 SERVER_URL=http://localhost:8000 和内置 Agent Token，
    负责反向监控 Server 自身。

    stdout/stderr 重定向到 ``LOG_DIR/local_agent.log``（append）而非 PIPE：
    PIPE 若无人读取，内核缓冲（约 64KB）写满后子进程会阻塞假死（日志多的场景必现）；
    落盘既防阻塞又保留 Agent 日志痕迹。返回 ``(process, log_handle)``，
    调用方负责在子进程退出后关闭 log_handle。

    显式注入 PYTHONPATH（探测到 agent 包父目录时）：生产环境 cwd 即 agent 父目录
    无需注入也能工作；开发环境 cwd=backend/ 必须注入，否则子进程秒崩
    ModuleNotFoundError。探测不到时记 warning 并维持现状启动（不 fail-closed）。
    """
    log_handle = None
    try:
        log_dir = Path(settings.LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "local_agent.log"
        log_handle = open(log_path, "a", encoding="utf-8")
        env = None
        agent_parent = _resolve_agent_pythonpath()
        if agent_parent is None:
            logger.warning(
                "未探测到 agent 包父目录，本地 Agent 将依赖 cwd 解析 "
                "agent.app.local_main（开发环境可能启动失败）"
            )
        else:
            existing = os.environ.get("PYTHONPATH", "")
            parts = [agent_parent, *(p for p in existing.split(os.pathsep) if p)]
            env = {**os.environ, "PYTHONPATH": os.pathsep.join(parts)}
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent.app.local_main",
                "--server-url=http://localhost:8000",
                f"--agent-token={agent_token}",
                f"--check-interval={settings.LOCAL_AGENT_CHECK_INTERVAL}",
                f"--health-timeout={settings.LOCAL_AGENT_HEALTH_TIMEOUT}",
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
        )
        logger.info(f"本地 Agent 已启动，PID: {process.pid}，日志输出至 {log_path}")
        return process, log_handle
    except Exception as e:
        if log_handle is not None:
            log_handle.close()
        logger.error(f"启动本地 Agent 失败: {e}")
        return None, None


docs_config = {}
if not settings.DOCS_ENABLED:
    docs_config = {"docs_url": None, "openapi_url": None, "redoc_url": None}

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    debug=settings.DEBUG,
    lifespan=lifespan,
    **docs_config,
)


@app.middleware("http")
async def request_body_limit_middleware(request, call_next):
    """全局请求体上限（DoS 防护）：基于 Content-Length 在读 body 前拦截超限请求。

    认证 Agent 理论上可发送超大 metrics payload 打爆内存；此中间件仅读取
    Content-Length 头（不读 body），超过 METRIC_INGEST_MAX_BODY_BYTES 直接 413，
    使上限真正生效。无 Content-Length（chunked）的请求交由各端点自身上限兜底。
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            size = int(content_length)
        except ValueError:
            size = 0
        if size > settings.METRIC_INGEST_MAX_BODY_BYTES:
            return JSONResponse(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                content={
                    "detail": (
                        f"请求体超过上限（{settings.METRIC_INGEST_MAX_BODY_BYTES} 字节），"
                        "请减少 samples 数量或拆分为多批上报"
                    )
                },
            )
    return await call_next(request)


@app.middleware("http")
async def open_api_audit_middleware(request, call_next):
    """开放 API 调用审计：仅对已认证（request.state 含 key_id）的请求落审计。

    仅记录 key_id/user/method/path/status 等轻量字段，不记录请求/响应体；
    写库失败被隔离，绝不影响主流程响应。
    """
    response = await call_next(request)
    try:
        path = request.url.path
        if path.startswith("/api/open/v1"):
            key_id = getattr(request.state, "api_key_id", None)
            user_id = getattr(request.state, "api_user_id", None)
            if key_id is not None:
                with Session(database_module.engine) as audit_session:
                    audit_log.record_open_api_call(
                        audit_session,
                        key_id=key_id,
                        user_id=user_id,
                        method=request.method,
                        path=path,
                        status_code=response.status_code,
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"开放 API 审计写入失败（已忽略）: {exc}")
    return response

app.include_router(auth_router)
app.include_router(agents_router)
app.include_router(users_router)
app.include_router(nodes_router)
app.include_router(node_groups_router)
app.include_router(metrics_router)
app.include_router(alert_rules_router)
app.include_router(alert_silences_router)
app.include_router(alert_inhibitions_router)
app.include_router(alerts_router)
app.include_router(heal_actions_router)
app.include_router(heal_rules_router)
app.include_router(heal_tasks_router)
app.include_router(incidents_router)
app.include_router(agent_tasks_router)
app.include_router(backup_router)
app.include_router(admin_router)
app.include_router(admin_metric_backend_router)
app.include_router(notification_channels_router)
app.include_router(notification_templates_router)
if HAS_REMOTE_EXECUTION:
    app.include_router(remote_execution_router)
app.include_router(dashboard_router)
app.include_router(websocket_router)
if HAS_AI_HEAL:
    app.include_router(ai_heal_router)
if HAS_AI_TOOLS:
    app.include_router(ai_tools_router)
if HAS_OPTIMIZATIONS:
    app.include_router(optimizations_router)
app.include_router(license_router)
app.include_router(license_claim_router)
app.include_router(agent_version_router)
app.include_router(agent_version_admin_router)
if HAS_API_KEYS:
    app.include_router(api_keys_admin_router)
# 开放 API（/api/open/v1）：受 OPEN_API_ENABLED 总开关控制，关闭后路由不注册（404）。
if settings.OPEN_API_ENABLED and HAS_OPEN_API:
    app.include_router(open_api_router)
if settings.OPEN_API_ENABLED and HAS_WEBHOOKS:
    app.include_router(webhooks_router)


@app.get("/health")
def health_check() -> dict:
    """服务健康检查接口。"""
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
    }


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """全局异常处理：记录日志并返回 500。"""
    logger.exception(f"未捕获异常: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "内部错误"},
    )
