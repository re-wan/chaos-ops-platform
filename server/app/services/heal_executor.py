"""自愈执行引擎。

负责告警触发后的自愈规则匹配、模板渲染、白名单确认、任务创建与执行编排。
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import Session, select

from app.core.config import settings
from app.core.logger import get_logger
from app.models.alert_rule import AlertRule
from app.models.heal_action import HealAction
from app.models.heal_rule import HealRule
from app.models.heal_task import HealTask
from app.models.heal_whitelist import HealAutoApproveWhitelist
from app.models.node import Node
from app.core.incident_event_bus import record_incident_event
from app.services import heal_action as heal_action_service, heal_verification
from app.services.incident import find_active_incident_for_alert
from app.services.heal_task_queue import (
    approve_task,
    create_task,
    reject_task,
    retry_task,
    start_task,
)

logger = get_logger("services.heal_executor")

# 模板变量正则：{{ variable }}
_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# 执行超时默认 120 秒
_DEFAULT_EXECUTION_TIMEOUT_SECONDS = 120
# running 卡死清扫宽限默认 60 秒
_DEFAULT_RUNNING_SWEEP_GRACE_SECONDS = 60


def _record_heal_event(
    session: Session,
    subtype: str,
    title: str,
    description: Optional[str],
    rule_id: int,
    node_id: str,
    metadata: Optional[dict] = None,
    created_by: Optional[int] = None,
) -> None:
    """查找关联 Incident 并写入自愈时间线事件；无关联事件则跳过。"""
    incident = find_active_incident_for_alert(session, rule_id, node_id)
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
        created_by=created_by,
    )


def get_execution_timeout_seconds() -> int:
    """返回执行超时秒数。"""
    return getattr(settings, "HEAL_EXECUTION_TIMEOUT_SECONDS", _DEFAULT_EXECUTION_TIMEOUT_SECONDS)


def get_running_sweep_grace_seconds() -> int:
    """返回 running 卡死清扫的宽限秒数。

    与执行超时叠加使用：仅当 ``started_at`` 早于 ``超时 + 宽限`` 时才视为卡死，
    避免误清扫刚启动、仍在正常执行窗口内的任务。
    """
    return getattr(
        settings,
        "HEAL_RUNNING_SWEEP_GRACE_SECONDS",
        _DEFAULT_RUNNING_SWEEP_GRACE_SECONDS,
    )


def _render_template(value: Any, context: dict) -> Any:
    """递归渲染模板变量。

    渲染失败时保留原字符串，不中断执行。
    """
    if isinstance(value, str):
        def replacer(match: re.Match) -> str:
            key = match.group(1)
            if key in context:
                return str(context[key])
            # 变量不存在时保留原样
            return match.group(0)

        return _TEMPLATE_PATTERN.sub(replacer, value)

    if isinstance(value, dict):
        return {k: _render_template(v, context) for k, v in value.items()}

    if isinstance(value, list):
        return [_render_template(item, context) for item in value]

    return value


def _validate_params_against_schema(params: dict, schema: dict, path: str = "") -> None:
    """根据 JSON Schema 校验参数。

    MVP 阶段支持 type、required、minimum、maximum、items、anyOf。
    """
    if not isinstance(params, dict):
        raise ValueError("参数必须是 JSON 对象")

    schema_type = schema.get("type")
    if schema_type == "object":
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        for key in required:
            if key not in params:
                raise ValueError(f"缺少必填参数: {key}")
        for key, value in params.items():
            if key in properties:
                _validate_value(value, properties[key], f"{path}.{key}")
    elif schema_type is not None:
        _validate_value(params, schema, path)

    any_of = schema.get("anyOf")
    if any_of and schema_type is None:
        errors = []
        for sub_schema in any_of:
            try:
                _validate_params_against_schema(params, sub_schema, path)
                return
            except ValueError as e:
                errors.append(str(e))
        raise ValueError(f"参数不满足 anyOf 任一条件: {'; '.join(errors)}")


def _validate_value(value: Any, schema: dict, path: str) -> None:
    """校验单个值。"""
    schema_type = schema.get("type")
    if schema_type == "string" and not isinstance(value, str):
        raise ValueError(f"{path} 必须是字符串")
    if schema_type == "integer" and not isinstance(value, int):
        raise ValueError(f"{path} 必须是整数")
    if schema_type == "number" and not isinstance(value, (int, float)):
        raise ValueError(f"{path} 必须是数字")
    if schema_type == "boolean" and not isinstance(value, bool):
        raise ValueError(f"{path} 必须是布尔值")
    if schema_type == "array" and not isinstance(value, list):
        raise ValueError(f"{path} 必须是数组")

    if schema_type == "array":
        items_schema = schema.get("items", {})
        for idx, item in enumerate(value):
            _validate_value(item, items_schema, f"{path}[{idx}]")

    if isinstance(value, (int, float)):
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            raise ValueError(f"{path} 不能小于 {minimum}")
        maximum = schema.get("maximum")
        if maximum is not None and value > maximum:
            raise ValueError(f"{path} 不能大于 {maximum}")


def _build_template_context(
    session: Session,
    alert_rule: AlertRule,
    node_id: str,
    labels: dict,
    metric_context: Optional[dict],
) -> dict:
    """构建模板变量上下文。"""
    node = session.exec(select(Node).where(Node.node_id == node_id)).first()
    node_name = node.name if node else node_id

    context = {
        "node_id": node_id,
        "node_name": node_name,
        "alert_rule_name": alert_rule.name,
        "severity": alert_rule.severity,
    }

    if metric_context:
        context["metric_name"] = metric_context.get("metric_name", "")
        context["metric_value"] = metric_context.get("metric_value", "")

    # labels 中的变量优先级低于专用变量
    context.update(labels)
    return context


def _is_whitelisted(
    session: Session,
    node_id: str,
    action_id: str,
) -> bool:
    """检查 (node_id, action_id) 是否在白名单中且启用。"""
    entry = session.exec(
        select(HealAutoApproveWhitelist).where(
            HealAutoApproveWhitelist.node_id == node_id,
            HealAutoApproveWhitelist.action_id == action_id,
            HealAutoApproveWhitelist.enabled == True,  # noqa: E712
        )
    ).first()
    return entry is not None


def _has_running_task_for_node_action(
    session: Session,
    node_id: str,
    action_id: str,
) -> bool:
    """检查同一节点同一动作是否有运行中任务，实现串行执行。"""
    running = session.exec(
        select(HealTask).where(
            HealTask.node_id == node_id,
            HealTask.action_id == action_id,
            HealTask.status == "running",
        )
    ).first()
    return running is not None


def handle_alert_firing(
    session: Session,
    alert_rule: AlertRule,
    node_id: str,
    labels: Optional[dict] = None,
    metric_context: Optional[dict] = None,
) -> Optional[HealTask]:
    """告警 firing 后的自愈执行入口。

    匹配启用的 HealRule，渲染参数，校验，创建任务或自动执行。
    异常被捕获隔离，不影响告警通知流程。

    Returns:
        创建的任务，或 None（未匹配/未触发）。
    """
    try:
        return _handle_alert_firing_internal(
            session, alert_rule, node_id, labels, metric_context
        )
    except Exception as exc:  # noqa: BLE001
        # 显式回滚：防止共享 Session 因异常进入 inactive，污染后续告警通知/重试；
        # 与 alert_detector 共享 session 的失败处理口径一致。
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.exception(f"自愈执行引擎处理 firing 失败: {exc}")
        return None


def _handle_alert_firing_internal(
    session: Session,
    alert_rule: AlertRule,
    node_id: str,
    labels: Optional[dict],
    metric_context: Optional[dict],
) -> Optional[HealTask]:
    """内部实现。"""
    # 1. 查找启用的自愈规则
    heal_rule = session.exec(
        select(HealRule).where(
            HealRule.alert_rule_id == alert_rule.id,
            HealRule.enabled == True,  # noqa: E712
        )
    ).first()

    if heal_rule is None:
        logger.debug(
            f"未找到告警规则 {alert_rule.id} 对应的自愈规则，跳过"
        )
        return None

    # 2. 加载动作
    action = session.exec(
        select(HealAction).where(HealAction.action_id == heal_rule.action_id)
    ).first()
    if action is None:
        logger.warning(
            f"自愈规则引用的动作不存在: rule_id={heal_rule.id}, "
            f"action_id={heal_rule.action_id}"
        )
        return None

    # 3. 脚本类动作（custom_script / ai_generated_script）必须已审批且 hash 校验通过
    if action.action_type in heal_action_service.SCRIPT_ACTION_TYPES:
        if not action.is_approved:
            logger.warning(
                f"脚本未审批，跳过执行: action_id={action.action_id}"
            )
            return None
        if not heal_action_service.verify_script_hash(action):
            logger.warning(
                f"脚本 hash 校验失败，跳过执行: action_id={action.action_id}"
            )
            return None

    # 4. 渲染模板变量
    context = _build_template_context(
        session, alert_rule, node_id, labels or {}, metric_context
    )
    try:
        raw_params = json.loads(heal_rule.action_params or "{}")
    except json.JSONDecodeError:
        raw_params = {}
    rendered_params = _render_template(raw_params, context)

    # 5. 校验参数 Schema
    try:
        parameter_schema = json.loads(action.parameter_schema)
        _validate_params_against_schema(rendered_params, parameter_schema)
    except ValueError as exc:
        logger.warning(
            f"动作参数校验失败: rule_id={heal_rule.id}, action_id={action.action_id}, "
            f"error={exc}"
        )
        return None

    # 6. 判断是否需要确认
    requires_approval = True
    if action.risk_level != "high" and _is_whitelisted(session, node_id, action.action_id):
        requires_approval = False

    # 7. 检查串行执行
    if _has_running_task_for_node_action(session, node_id, action.action_id):
        logger.info(
            f"同一节点同一动作已有运行中任务，本次跳过: "
            f"node_id={node_id}, action_id={action.action_id}"
        )
        return None

    # 7b. 5 分钟去重（Phase 3 Step 05，设计 §4.3）：同一 (node, action) 在窗口内
    # 不重复下发。auto 模式下仅 Redis 可用时启用，单进程/测试默认关闭。
    from app.services import heal_dedup

    if heal_dedup.is_dedup_enabled() and heal_dedup.should_dedup(
        node_id, action.action_id
    ):
        logger.info(
            f"自愈去重：去重窗口内已下发过相同动作，本次跳过: "
            f"node_id={node_id}, action_id={action.action_id}"
        )
        return None

    # 8. 创建任务，复制验证配置到任务
    task = create_task(
        session=session,
        node_id=node_id,
        alert_rule_id=alert_rule.id,
        heal_rule_id=heal_rule.id,
        action_id=action.action_id,
        action_params=rendered_params,
        risk_level=action.risk_level,
        requires_approval=requires_approval,
        verification_config=heal_rule.verification_config,
    )

    logger.info(
        f"创建自愈任务: task_id={task.task_id}, node_id={node_id}, "
        f"action_id={action.action_id}, requires_approval={requires_approval}"
    )

    # 记录去重窗口（启用时），窗口期内同 (node, action) 不再重复下发。
    if heal_dedup.is_dedup_enabled():
        heal_dedup.record_dispatched(node_id, action.action_id)

    _record_heal_event(
        session=session,
        subtype="heal.pending",
        title="timeline.heal.pending.title",
        description="timeline.heal.pending.desc",
        rule_id=alert_rule.id,
        node_id=node_id,
        metadata={
            "task_id": task.task_id,
            "action_id": action.action_id,
            "requires_approval": requires_approval,
        },
    )

    # 9. 无需确认则自动进入运行状态
    if not requires_approval and heal_rule.auto_execute:
        _dispatch_task(session, task, executed_by="auto")

    return task


def _dispatch_task(
    session: Session,
    task: HealTask,
    executed_by: str,
) -> HealTask:
    """下发任务给 Agent。MVP 阶段仅更新状态为 running。"""
    task = start_task(session, task, executed_by=executed_by)
    logger.info(
        f"下发自愈任务: task_id={task.task_id}, executed_by={executed_by}"
    )

    _record_heal_event(
        session=session,
        subtype="heal.executed",
        title="timeline.heal.executed.title",
        description="timeline.heal.executed.desc",
        rule_id=task.alert_rule_id,
        node_id=task.node_id,
        metadata={
            "task_id": task.task_id,
            "action_id": task.action_id,
            "executed_by": executed_by,
        },
    )
    return task


def approve_and_execute(
    session: Session,
    task: HealTask,
    approved_by: int,
    add_to_whitelist: bool = False,
) -> HealTask:
    """确认并执行待确认任务。

    Args:
        add_to_whitelist: 是否同时将 (node_id, action_id) 加入白名单。
    """
    task = approve_task(session, task, approved_by=approved_by)

    _record_heal_event(
        session=session,
        subtype="heal.approved",
        title="timeline.heal.approved.title",
        description="timeline.heal.approved.desc",
        rule_id=task.alert_rule_id,
        node_id=task.node_id,
        metadata={
            "task_id": task.task_id,
            "action_id": task.action_id,
            "approved_by": approved_by,
        },
        created_by=approved_by,
    )

    if add_to_whitelist:
        _add_to_whitelist(session, task.node_id, task.action_id, approved_by)

    return _dispatch_task(session, task, executed_by="manual")


def reject_execution(
    session: Session,
    task: HealTask,
    rejected_by: int,
    reason: Optional[str] = None,
) -> HealTask:
    """拒绝待确认任务，保留记录。"""
    task = reject_task(session, task, rejected_by=rejected_by, reason=reason)

    _record_heal_event(
        session=session,
        subtype="heal.rejected",
        title="timeline.heal.rejected.title",
        description=reason or "timeline.heal.rejected.desc",
        rule_id=task.alert_rule_id,
        node_id=task.node_id,
        metadata={
            "task_id": task.task_id,
            "action_id": task.action_id,
            "rejected_by": rejected_by,
            "reason": reason,
        },
        created_by=rejected_by,
    )
    return task


def retry_failed_task(
    session: Session,
    task: HealTask,
) -> HealTask:
    """重试失败/超时任务（手动触发）。

    手动重试重置 retry_count，允许重新走完整重试流程。
    """
    task.retry_count = 0
    session.add(task)
    session.commit()
    return retry_task(session, task)


def _add_to_whitelist(
    session: Session,
    node_id: str,
    action_id: str,
    created_by: int,
) -> HealAutoApproveWhitelist:
    """将 (node_id, action_id) 加入白名单。"""
    existing = session.exec(
        select(HealAutoApproveWhitelist).where(
            HealAutoApproveWhitelist.node_id == node_id,
            HealAutoApproveWhitelist.action_id == action_id,
        )
    ).first()

    if existing is not None:
        existing.enabled = True
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    entry = HealAutoApproveWhitelist(
        node_id=node_id,
        action_id=action_id,
        enabled=True,
        created_by=created_by,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)

    logger.info(
        f"加入自愈白名单: node_id={node_id}, action_id={action_id}, "
        f"created_by={created_by}"
    )
    return entry


def record_task_result(
    session: Session,
    task: HealTask,
    success: bool,
    output: Optional[str] = None,
    message: Optional[str] = None,
    error_message: Optional[str] = None,
) -> HealTask:
    """记录 Agent 上报的任务执行结果。"""
    now = datetime.now(timezone.utc)
    task.finished_at = now
    task.updated_at = now
    task.status = "success" if success else "failed"
    task.result = json.dumps(
        {
            "success": success,
            "output": output,
            "message": message,
        },
        ensure_ascii=False,
    )
    task.error_message = error_message
    session.add(task)
    session.commit()
    session.refresh(task)

    logger.info(
        f"自愈任务执行结果: task_id={task.task_id}, status={task.status}, "
        f"success={success}"
    )

    # 成功后安排效果验证，失败后进入重试决策
    if success:
        heal_verification.schedule_verification(session, task)
    else:
        heal_verification.handle_execution_failure(session, task)

    return task
