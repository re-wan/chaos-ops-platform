"""后台任务调度器，带异常隔离。"""

import functools
import threading
import traceback
from typing import Callable

from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("scheduler")

# 全局调度器实例
_scheduler: AsyncIOScheduler | None = None


def safe_task(func: Callable) -> Callable:
    """装饰器：捕获任务异常并记录日志，防止单个任务拖垮 Server。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"后台任务 {func.__name__} 执行失败: {e}\n{traceback.format_exc()}")

    return wrapper


def get_scheduler() -> AsyncIOScheduler:
    """获取或创建全局 APScheduler 实例。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")

        def _on_job_error(event):
            logger.error(f"调度任务异常: {event.exception}")

        _scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    return _scheduler


def setup_scheduler() -> AsyncIOScheduler:
    """配置并返回调度器。

    MVP 阶段默认任务：
        - 每天 02:00 执行数据库备份
        - 每天 03:00 执行日志/数据清理
    """
    scheduler = get_scheduler()

    # 延迟导入，避免循环依赖
    from app.tasks.backup_task import backup_database_task
    from app.tasks.cleanup_task import cleanup_task
    from app.tasks.detector_task import (
        cleanup_metric_cache_task,
        reload_detector_rules_task,
    )
    from app.tasks.notification_task import retry_pending_notifications_task
    # 防御式 import：三版物理分包的免费/专业版会删除 optimization_task.py；
    # 文件存在时（企业版/dev）行为不变，缺失时跳过 AI 自优化相关 job。
    try:
        from app.tasks.optimization_task import (
            optimization_effect_track_task,
            weekly_optimization_task,
        )

        HAS_OPTIMIZATION_TASK = True
    except ImportError:
        optimization_effect_track_task = None
        weekly_optimization_task = None
        HAS_OPTIMIZATION_TASK = False
    # 防御式 import：免费版物理分包删除 analysis_job_task.py（AI 自动分析队列），
    # 文件存在时（专业/企业版/dev）注册消费者，缺失时跳过。
    try:
        from app.tasks.analysis_job_task import process_analysis_jobs

        HAS_ANALYSIS_JOB_TASK = True
    except ImportError:
        process_analysis_jobs = None
        HAS_ANALYSIS_JOB_TASK = False
    from app.tasks.verify_heal_task import run_verification_checks
    from app.tasks.incident_task import auto_close_resolved_incidents
    from app.tasks.node_status_task import check_offline_nodes_task

    scheduler.add_job(
        check_offline_nodes_task,
        trigger="interval",
        seconds=60,
        id="check_offline_nodes",
        name="节点离线状态检查",
        replace_existing=True,
    )

    scheduler.add_job(
        auto_close_resolved_incidents,
        trigger=CronTrigger(hour=4, minute=0),
        id="auto_close_resolved_incidents",
        name="自动关闭已解决事件",
        replace_existing=True,
    )

    scheduler.add_job(
        reload_detector_rules_task,
        trigger="interval",
        seconds=settings.ALERT_DETECTOR_RELOAD_INTERVAL_SECONDS,
        id="reload_detector_rules",
        name="告警规则索引定时刷新",
        replace_existing=True,
    )
    scheduler.add_job(
        cleanup_metric_cache_task,
        trigger="interval",
        seconds=60,
        id="cleanup_metric_cache",
        name="告警指标缓存定时清理",
        replace_existing=True,
    )

    scheduler.add_job(
        backup_database_task,
        trigger=CronTrigger(hour=2, minute=0),
        id="backup_database",
        name="数据库定时备份",
        replace_existing=True,
    )
    scheduler.add_job(
        cleanup_task,
        trigger=CronTrigger(hour=3, minute=0),
        id="cleanup",
        name="日志与数据清理",
        replace_existing=True,
    )

    scheduler.add_job(
        run_verification_checks,
        trigger="interval",
        seconds=10,
        id="verify_heal_tasks",
        name="自愈效果验证检查",
        replace_existing=True,
    )

    scheduler.add_job(
        retry_pending_notifications_task,
        trigger="interval",
        seconds=30,
        id="retry_pending_notifications",
        name="通知失败重试轮询",
        replace_existing=True,
    )

    # AI 根因分析队列消费者：每 5s 单步消费（max_instances=1 保证串行）
    if HAS_ANALYSIS_JOB_TASK:
        scheduler.add_job(
            process_analysis_jobs,
            trigger="interval",
            seconds=5,
            id="process_analysis_jobs",
            name="AI 根因分析队列消费",
            replace_existing=True,
            max_instances=1,
        )

    # Phase 3 Step 01：AI 自我优化周分析（每周日 03:30，避开 03:00 清理任务）
    # Phase 3 Step 03：优化效果追踪（每日 03:45）——均属企业版 AI 自优化，文件缺失时跳过。
    if HAS_OPTIMIZATION_TASK:
        scheduler.add_job(
            weekly_optimization_task,
            trigger=CronTrigger(day_of_week="sun", hour=3, minute=30),
            id="weekly_ai_optimization",
            name="AI 自我优化周分析",
            replace_existing=True,
        )
        scheduler.add_job(
            optimization_effect_track_task,
            trigger=CronTrigger(hour=3, minute=45),
            id="optimization_effect_track",
            name="优化效果追踪",
            replace_existing=True,
        )

    return scheduler


def start_scheduler() -> None:
    """启动后台任务调度器。"""
    scheduler = setup_scheduler()
    if not scheduler.running:
        scheduler.start()
        logger.info("后台任务调度器已启动")


def shutdown_scheduler(timeout: float = 10.0) -> None:
    """关闭后台任务调度器，优雅等待运行中任务结束。

    使用 ``wait=True`` 等待正在执行的备份/清理/通知重试等任务完成，避免直接杀掉
    进行中的周期任务；同时用 ``timeout`` 限制最长等待时间，超时则记录 warning 并
    强制结束关闭流程（运行中任务随进程退出被回收），防止关机无限阻塞。

    Args:
        timeout: 最长等待秒数，默认 10s。
    """
    global _scheduler
    if _scheduler is None or not _scheduler.running:
        return

    scheduler = _scheduler
    done = threading.Event()

    def _do_shutdown() -> None:
        try:
            scheduler.shutdown(wait=True)
        finally:
            done.set()

    # 在独立线程中执行阻塞式关闭，便于用 timeout 限制总等待时间。
    worker = threading.Thread(target=_do_shutdown, name="scheduler-shutdown", daemon=True)
    worker.start()
    if not done.wait(timeout):
        logger.warning(
            f"调度器关闭超过 {timeout}s，仍有任务在运行，将强制结束关闭流程"
        )
    else:
        logger.info("后台任务调度器已优雅关闭")
    _scheduler = None
