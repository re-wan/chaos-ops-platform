"""自愈效果验证与重试决策。

负责自愈动作执行后的退出码验证、指标恢复验证、失败重试与结果记录。
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlmodel import Session

from app.core.incident_event_bus import record_incident_event
from app.core.logger import get_logger
from app.services.incident import find_active_incident_for_alert
from app.models.heal_task import HealTask
from app.services.metric_query import execute_query, get_latest_value

logger = get_logger("services.heal_verification")

# 验证配置默认值
_DEFAULT_VERIFY_WINDOW_SECONDS = 120
_DEFAULT_RETRY_TIMES = 2
_DEFAULT_RETRY_INTERVAL_SECONDS = 30

# 支持的比较操作符
_SUPPORTED_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

# 支持的指标聚合方式
_SUPPORTED_AGGREGATORS = {"last", "avg", "max", "min"}


def _load_verification_config(task: HealTask) -> dict:
    """加载任务或关联规则的验证配置，并填充默认值。"""
    config: dict = {}
    if task.verification_config:
        try:
            config = json.loads(task.verification_config)
        except json.JSONDecodeError:
            config = {}

    return {
        "verify_exit_code": config.get("verify_exit_code", True),
        "verify_metric": config.get("verify_metric"),
        "verify_window_seconds": config.get(
            "verify_window_seconds", _DEFAULT_VERIFY_WINDOW_SECONDS
        ),
        "retry_times": config.get("retry_times", _DEFAULT_RETRY_TIMES),
        "retry_interval_seconds": config.get(
            "retry_interval_seconds", _DEFAULT_RETRY_INTERVAL_SECONDS
        ),
    }


def _now() -> datetime:
    """返回当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def schedule_verification(session: Session, task: HealTask) -> HealTask:
    """执行成功后安排效果验证。

    从 HealRule 读取 verification_config（任务创建时已复制到 task），
    设置 verification_status=pending 与 verification_due_at。
    """
    config = _load_verification_config(task)
    now = _now()

    task.verification_status = "pending"
    task.verification_due_at = now + timedelta(
        seconds=config["verify_window_seconds"]
    )
    task.max_retries = config["retry_times"]
    task.updated_at = now

    session.add(task)
    session.commit()
    session.refresh(task)

    logger.info(
        f"安排自愈验证: task_id={task.task_id}, "
        f"due_at={task.verification_due_at.isoformat()}"
    )
    return task


def handle_execution_failure(session: Session, task: HealTask) -> HealTask:
    """Agent 执行失败后的处理入口。

    直接进入重试决策；若还有重试次数则重置为 approved 并设置 scheduled_at，
    否则标记为最终失败。
    """
    config = _load_verification_config(task)
    task.max_retries = config["retry_times"]
    return _decide_retry(session, task, "Agent 执行失败")


def handle_execution_timeout(session: Session, task: HealTask) -> HealTask:
    """运行中超时卡死后的处理入口。

    供定时清扫任务在 ``mark_task_timeout`` 之后调用：先写一条 ``heal.timeout``
    审计事件保留超时原因，再复用现有重试决策（``_decide_retry``）：可重试则
    回 approved 并设置 scheduled_at 等待重新下发，达上限则标记 failed。
    不新造状态流转，与执行失败共用同一套重试语义。
    """
    config = _load_verification_config(task)
    task.max_retries = config["retry_times"]

    _record_heal_event(
        session=session,
        subtype="heal.timeout",
        title="timeline.heal.timeout.title",
        description="timeline.heal.timeout.desc",
        task=task,
        metadata={
            "task_id": task.task_id,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "reason": "执行超时",
        },
    )
    return _decide_retry(session, task, "执行超时")


def _reset_for_retry(task: HealTask) -> None:
    """重置任务执行相关字段，为重试做准备。

    保留 verification_result，便于审计本次失败原因；清空 verification_status
    与 due_at，等待下次执行后重新验证。
    """
    task.status = "approved"
    task.started_at = None
    task.finished_at = None
    task.result = None
    task.executed_by = None
    task.error_message = None
    task.verification_status = None
    task.verification_due_at = None


def _record_heal_event(
    session: Session,
    subtype: str,
    title: str,
    description: Optional[str],
    task: HealTask,
    metadata: Optional[dict] = None,
) -> None:
    """查找关联 Incident 并写入自愈时间线事件；无关联事件则跳过。"""
    incident = find_active_incident_for_alert(
        session, task.alert_rule_id, task.node_id
    )
    if incident is None:
        return

    record_incident_event(
        session=session,
        incident_id=incident.id,
        event_type="heal",
        event_subtype=subtype,
        title=title,
        description=description,
        metadata=metadata,
        source="executor",
    )


def _decide_retry(
    session: Session, task: HealTask, reason: str
) -> HealTask:
    """根据已重试次数决定是否继续重试。"""
    config = _load_verification_config(task)
    now = _now()

    if task.retry_count < task.max_retries:
        task.retry_count += 1
        _reset_for_retry(task)
        task.scheduled_at = now + timedelta(
            seconds=config["retry_interval_seconds"]
        )
        task.updated_at = now
        session.add(task)
        session.commit()
        session.refresh(task)

        logger.info(
            f"自愈任务进入重试: task_id={task.task_id}, "
            f"retry_count={task.retry_count}, max_retries={task.max_retries}, "
            f"scheduled_at={task.scheduled_at.isoformat()}"
        )
        return task

    task.status = "failed"
    task.verification_status = "failed"
    task.error_message = f"达到最大重试次数，自愈失败: {reason}"
    task.updated_at = now
    session.add(task)
    session.commit()
    session.refresh(task)

    _record_heal_event(
        session=session,
        subtype="heal.failed",
        title="timeline.heal.failed.title",
        description="timeline.heal.failed.desc",
        task=task,
        metadata={
            "task_id": task.task_id,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "reason": reason,
        },
    )

    logger.error(
        f"自愈任务最终失败: task_id={task.task_id}, "
        f"retry_count={task.retry_count}, reason={reason}"
    )
    return task


def _check_exit_code(task: HealTask, config: dict) -> tuple[bool, Optional[str]]:
    """退出码验证。

    Returns:
        (是否通过, 失败原因或 None)
    """
    if not config["verify_exit_code"]:
        return True, None

    try:
        result = json.loads(task.result or "{}")
    except json.JSONDecodeError:
        return False, "执行结果不是合法 JSON"

    if not result.get("success", False):
        return False, "Agent 报告执行失败"

    return True, None


def _query_metric_value(
    task: HealTask, metric_config: dict
) -> Optional[Any]:
    """查询指标并按 aggregator 获取代表值。

    优先使用 execute_query 获取时间序列，再根据 aggregator 计算；
    aggregator=last 时直接使用 get_latest_value 减少查询量。
    """
    metric = metric_config.get("metric")
    labels = metric_config.get("labels", {})
    aggregator = metric_config.get("aggregator", "last")
    verify_window = metric_config.get(
        "verify_window_seconds", _DEFAULT_VERIFY_WINDOW_SECONDS
    )

    if aggregator not in _SUPPORTED_AGGREGATORS:
        logger.warning(f"不支持的聚合方式: {aggregator}，使用 last")
        aggregator = "last"

    labels_str = json.dumps(labels, ensure_ascii=False) if labels else None

    if aggregator == "last":
        latest = get_latest_value(task.node_id, metric, labels_str)
        if latest is None:
            return None
        return latest["value"]

    # avg/max/min 需要查询时间窗口内的样本
    end = _now()
    start = end - timedelta(seconds=verify_window)
    result = execute_query(
        node_id=task.node_id,
        metric=metric,
        labels_str=labels_str,
        start_str=start.isoformat(),
        end_str=end.isoformat(),
        step_str=None,
        aggregator=aggregator,
    )
    data = result.get("data", [])
    if not data:
        return None

    values = [point["value"] for point in data]
    if aggregator == "avg":
        return sum(values) / len(values)
    if aggregator == "max":
        return max(values)
    if aggregator == "min":
        return min(values)

    return values[-1]["value"]


def _check_metric(task: HealTask, config: dict) -> tuple[bool, Optional[str]]:
    """指标恢复验证。

    Returns:
        (是否通过, 失败原因或 None)
    """
    metric_config = config.get("verify_metric")
    if metric_config is None:
        return True, None

    op = metric_config.get("op", "==")
    expected_value = metric_config.get("value")

    if op not in _SUPPORTED_OPS:
        return False, f"不支持的操作符: {op}"
    if expected_value is None:
        return False, "verify_metric 缺少 value"

    try:
        actual_value = _query_metric_value(task, metric_config)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"指标查询失败，视为验证失败: {exc}")
        return False, f"指标查询失败: {exc}"

    if actual_value is None:
        return False, "未查询到指标数据"

    try:
        passed = _SUPPORTED_OPS[op](actual_value, expected_value)
    except TypeError as exc:
        return False, f"指标比较异常: {exc}"

    if not passed:
        return (
            False,
            f"指标验证失败: {metric_config.get('metric')} {op} "
            f"{expected_value} (实际 {actual_value})",
        )

    return True, None


def verify_task(session: Session, task: HealTask) -> HealTask:
    """执行效果验证。

    1. 退出码验证。
    2. 指标恢复验证（如配置）。
    3. 任一失败进入重试决策。
    4. 全部通过标记 verification_status=success。
    """
    config = _load_verification_config(task)
    now = _now()

    exit_ok, exit_reason = _check_exit_code(task, config)
    metric_ok, metric_reason = _check_metric(task, config)

    verification_result = {
        "exit_code_ok": exit_ok,
        "metric_ok": metric_ok,
        "verify_exit_code": config["verify_exit_code"],
        "verify_metric": config.get("verify_metric"),
        "verified_at": now.isoformat(),
    }

    if not exit_ok:
        verification_result["failure_reason"] = exit_reason
        task.verification_result = json.dumps(
            verification_result, ensure_ascii=False
        )
        return _decide_retry(session, task, exit_reason or "退出码验证失败")

    if not metric_ok:
        verification_result["failure_reason"] = metric_reason
        task.verification_result = json.dumps(
            verification_result, ensure_ascii=False
        )
        return _decide_retry(session, task, metric_reason or "指标验证失败")

    # 验证通过
    task.verification_status = "success"
    task.verification_result = json.dumps(
        verification_result, ensure_ascii=False
    )
    task.updated_at = now
    session.add(task)
    session.commit()
    session.refresh(task)

    _record_heal_event(
        session=session,
        subtype="heal.success",
        title="timeline.heal.success.title",
        description="timeline.heal.success.desc",
        task=task,
        metadata={
            "task_id": task.task_id,
            "retry_count": task.retry_count,
            "verification_result": verification_result,
        },
    )

    logger.info(f"自愈验证通过: task_id={task.task_id}")
    return task


def get_verification_status(task: HealTask) -> dict:
    """获取任务验证状态的可序列化字典。"""
    result = None
    if task.verification_result:
        try:
            result = json.loads(task.verification_result)
        except json.JSONDecodeError:
            result = {"raw": task.verification_result}

    return {
        "verification_status": task.verification_status,
        "verification_result": result,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
    }
