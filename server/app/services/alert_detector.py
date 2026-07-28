"""告警检测引擎。

事件驱动：订阅 MetricIngestedEvent，维护规则索引与节点最近值缓存，
对相关规则求值并驱动 pending/firing/resolved 状态转换。
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# 断点续传样本的新鲜度阈值（秒）：超过则只存储不告警
STALE_SAMPLE_SKIP_SECONDS = 60
from typing import Any, Optional

from sqlalchemy import Engine
from sqlmodel import Session, select

from app.core.config import settings
from app.core.event_bus import publish
from app.core.events import (
    MetricIngestedEvent,
    subscribe_metric_ingested,
    unsubscribe_metric_ingested,
)
from app.core.logger import get_logger
from app.core.metric_cache import NodeMetricCache, get_metric_cache
from app.models.alert_rule import AlertRule
from app.models.alert_state import AlertState
from app.models.node_group import NodeGroup
from app.core.incident_event_bus import record_incident_event
from app.services import alert_inhibition, alert_silence
from app.services import incident as incident_service
from app.services import alert_rule as alert_rule_service
from app.services.alert_dedup import build_dedup_key, record_notification, should_notify
from app.services.alert_rule import (
    extract_metric_names,
    off_rule_changed,
    parse_condition,
)
from app.services.heal_executor import handle_alert_firing
from app.services import notification as notification_service
from app.services.alert_state import get_or_create_state, transition_state
from app.services.metrics_ingest import query_metrics
from app.services.rule_index import RuleIndex

logger = get_logger("services.alert_detector")

# 单条规则评估最大即时重试次数
_MAX_EVAL_RETRIES = 3

# 启动时规则初始加载的最大重试次数与退避基数（秒）
_MAX_INITIAL_LOAD_RETRIES = 3
_INITIAL_LOAD_RETRY_INTERVAL_SECONDS = 0.5


def _node_in_group(session: Session, group_id_str: Optional[str], node_id: str) -> bool:
    """判断 node_id 是否属于指定的节点组。

    节点组不存在或解析失败时返回 False，避免误触发。
    """
    if not group_id_str:
        return False
    try:
        group_id = int(group_id_str)
    except (TypeError, ValueError):
        logger.warning(f"节点组 ID 格式非法: {group_id_str!r}")
        return False

    group = session.get(NodeGroup, group_id)
    if group is None:
        return False

    try:
        members = json.loads(group.node_ids or "[]")
    except json.JSONDecodeError:
        logger.warning(f"节点组成员解析失败: group_id={group_id}")
        return False

    if not isinstance(members, list):
        return False
    return node_id in members


def _max_workers() -> int:
    """检测器线程池大小。"""
    return settings.ALERT_DETECTOR_WORKERS


class AlertDetector:
    """告警检测引擎。"""

    def __init__(
        self,
        engine: Engine,
        cache: Optional[NodeMetricCache] = None,
        executor: Optional[ThreadPoolExecutor] = None,
    ):
        self._engine = engine
        self._cache = cache or get_metric_cache()
        self._executor = executor or ThreadPoolExecutor(
            max_workers=_max_workers(),
            thread_name_prefix="alert-detector",
        )
        self._rule_index = RuleIndex()
        self._processed_count = 0
        self._error_count = 0
        self._reload_failure_count = 0
        self._last_latency_ms = 0.0

    @property
    def rules_indexed(self) -> int:
        """当前索引中的规则数量。"""
        return self._rule_index.rule_count

    @property
    def processed_count(self) -> int:
        return self._processed_count

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def reload_failure_count(self) -> int:
        """规则索引加载失败累计次数（用于可观测/告警）。"""
        return self._reload_failure_count

    @property
    def last_latency_ms(self) -> float:
        return self._last_latency_ms

    @property
    def cache(self) -> NodeMetricCache:
        return self._cache

    def start(self) -> None:
        """加载规则并订阅事件。

        初始加载是检测引擎的唯一规则来源（直到下次 CRUD 或定时刷新），
        失败时重试，仍失败则 ERROR 级告警——不允许静默空索引运行。
        """
        self._initial_load_rules()
        subscribe_metric_ingested(self.on_metric_ingested)
        logger.info("告警检测引擎已启动")

    def _initial_load_rules(self) -> None:
        """启动首次规则加载：失败重试，最终失败则明确告警。"""
        for attempt in range(1, _MAX_INITIAL_LOAD_RETRIES + 1):
            if self.reload_rules():
                return
            if attempt < _MAX_INITIAL_LOAD_RETRIES:
                time.sleep(_INITIAL_LOAD_RETRY_INTERVAL_SECONDS * attempt)
        logger.error(
            f"告警规则初始加载连续 {_MAX_INITIAL_LOAD_RETRIES} 次失败：规则索引为空，"
            "检测引擎将在无规则状态下运行直到下次 CRUD 或定时刷新成功，"
            "此期间所有告警都会漏报，请立即检查数据库连接"
        )

    def stop(self) -> None:
        """关闭检测器线程池并取消事件订阅。"""
        unsubscribe_metric_ingested(self.on_metric_ingested)
        self._executor.shutdown(wait=False)
        logger.info("告警检测引擎已关闭")

    def reload_rules(self) -> bool:
        """从数据库加载所有启用的规则并重建索引。

        失败时**保留旧索引**（不清空、不替换为半成品），记 ERROR 级日志并累计
        ``reload_failure_count`` 供告警使用——避免 DB 抖动导致删规则误告警、
        增规则漏告警。

        Returns:
            True 表示加载并重建成功，False 表示失败（旧索引继续服役）。
        """
        try:
            with Session(self._engine) as session:
                rules = session.exec(
                    select(AlertRule).where(AlertRule.enabled == True)  # noqa: E712
                ).all()
                self._build_index(rules)
        except Exception as exc:  # noqa: BLE001
            self._reload_failure_count += 1
            logger.exception(
                f"加载告警规则失败，保留旧规则索引继续检测（可能含已删除规则或缺新增规则）: "
                f"{exc}"
            )
            return False
        return True

    def _build_index(self, rules: list[AlertRule]) -> None:
        """按 metric_name 建立规则倒排索引并预编译条件（委托 RuleIndex）。

        RuleIndex 在重建时完成 parse_condition（含 DSL/PromQL 校验），
        热路径评估直接复用预编译结果，避免每事件重复解析。
        """
        self._rule_index.rebuild(rules)

    def on_metric_ingested(self, event: MetricIngestedEvent) -> None:
        """事件订阅入口：异步提交检测任务。"""
        self._executor.submit(self._handle_event, event)

    def process_event(self, event: MetricIngestedEvent) -> None:
        """同步处理单个 MetricIngestedEvent（供 detector_worker 调用）。

        与 ``on_metric_ingested`` 的区别：本方法在当前线程同步执行 ``_handle_event``，
        适用于已被任务队列 worker 拉取后的场景，避免再经线程池二次跳转。
        """
        self._handle_event(event)

    def _handle_event(self, event: MetricIngestedEvent) -> None:
        """处理单个 MetricIngestedEvent。"""
        start = time.perf_counter()
        try:
            # 样本新鲜度检查：断点续传的历史样本（时间戳距现在超过阈值）只存储、
            # 不触发实时告警评估——避免补发旧数据导致告警补报/误报。
            # 缓存照常更新（用于后续实时样本的基线对比），但不进入规则评估。
            sample_ts = event.timestamp
            if sample_ts.tzinfo is None:
                sample_ts = sample_ts.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - sample_ts).total_seconds()
            if age_seconds > STALE_SAMPLE_SKIP_SECONDS:
                self._cache.update(
                    node_id=event.node_id,
                    metric_name=event.metric_name,
                    labels=event.labels,
                    value=event.value,
                    timestamp=event.timestamp,
                )
                return

            self._cache.update(
                node_id=event.node_id,
                metric_name=event.metric_name,
                labels=event.labels,
                value=event.value,
                timestamp=event.timestamp,
            )

            metric_context = {
                "metric_name": event.metric_name,
                "metric_value": event.value,
            }
            rule_ids = self._get_rules_for_metric(event.metric_name)

            # 同一事件命中的多规则共享一个 Session，避免每条规则重复开连接；
            # state_cache 在同一事件内复用 AlertState，消除评估与 firing 阶段
            # 的重复查询。子调用失败时显式 rollback，保证规则间相互隔离。
            with Session(self._engine) as session:
                state_cache: dict = {}
                for rule_id in rule_ids:
                    self._evaluate_rule_with_retry(
                        rule_id,
                        event.node_id,
                        event.timestamp,
                        event.labels,
                        metric_context,
                        session=session,
                        state_cache=state_cache,
                    )

            self._processed_count += 1
        except Exception as exc:  # noqa: BLE001
            self._error_count += 1
            logger.exception(f"处理 MetricIngestedEvent 失败: {exc}")
        finally:
            self._last_latency_ms = (time.perf_counter() - start) * 1000

    def _get_rules_for_metric(self, metric_name: str) -> set[int]:
        return self._rule_index.rules_for(metric_name)

    def _evaluate_rule_with_retry(
        self,
        rule_id: int,
        node_id: str,
        now: datetime,
        labels: Optional[dict] = None,
        metric_context: Optional[dict] = None,
        *,
        session: Optional[Session] = None,
        state_cache: Optional[dict] = None,
    ) -> None:
        """带有限重试的规则评估。

        若传入 ``session``（同一事件多规则共享），失败时显式 rollback，
        避免一条规则失败把会话卡在 inactive 而污染后续规则/重试。
        """
        for attempt in range(1, _MAX_EVAL_RETRIES + 1):
            try:
                self._evaluate_rule_for_node(
                    rule_id,
                    node_id,
                    now,
                    labels,
                    metric_context,
                    session=session,
                    state_cache=state_cache,
                )
                return
            except Exception as exc:  # noqa: BLE001
                if session is not None:
                    try:
                        session.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                logger.warning(
                    f"规则评估失败 (attempt {attempt}/{_MAX_EVAL_RETRIES}): "
                    f"rule_id={rule_id}, node_id={node_id}, error={exc}"
                )
                if attempt < _MAX_EVAL_RETRIES:
                    time.sleep(0.05 * attempt)
        self._error_count += 1

    def _evaluate_rule_for_node(
        self,
        rule_id: int,
        node_id: str,
        now: datetime,
        labels: Optional[dict] = None,
        metric_context: Optional[dict] = None,
        *,
        session: Optional[Session] = None,
        state_cache: Optional[dict] = None,
    ) -> None:
        """对单个规则/节点求值并转换状态，并在进入 firing 时处理通知与自愈。

        若传入 ``session``，则复用外部会话（同事件多规则共享，减少连接抖动），
        并通过 ``state_cache`` 在同一事件内复用 AlertState；否则自行开启会话。
        """
        if session is not None:
            self._evaluate_rule_for_node_impl(
                session, rule_id, node_id, now, labels, metric_context, state_cache
            )
            return

        with Session(self._engine) as owned_session:
            self._evaluate_rule_for_node_impl(
                owned_session,
                rule_id,
                node_id,
                now,
                labels,
                metric_context,
                state_cache,
            )

    def _evaluate_rule_for_node_impl(
        self,
        session: Session,
        rule_id: int,
        node_id: str,
        now: datetime,
        labels: Optional[dict] = None,
        metric_context: Optional[dict] = None,
        state_cache: Optional[dict] = None,
    ) -> None:
        """单规则/节点求值的实际实现，复用调用方提供的 Session。"""
        rule = session.get(AlertRule, rule_id)
        if rule is None or not rule.enabled:
            return

        # 范围过滤：全局规则对所有节点生效；node 规则仅对 scope_target 生效
        if rule.scope == "node" and rule.scope_target != node_id:
            return
        if rule.scope == "node_group" and not _node_in_group(
            session, rule.scope_target, node_id
        ):
            return

        # 优先复用 RuleIndex 预编译的条件，避免每事件重复解析 DSL/PromQL；
        # 索引中缺失（如规则刚启用尚未重建）时回退到即时解析，保证不漏评。
        condition = self._rule_index.get_parsed(rule.id)
        if condition is None:
            condition = parse_condition(rule.condition_type, rule.condition)
        condition_met = _evaluate_condition(condition, node_id, self._cache)

        # 同一事件内复用 AlertState，避免评估阶段与 firing 通知阶段重复查询。
        state_key = (rule.id, node_id)
        if state_cache is not None and state_key in state_cache:
            state = state_cache[state_key]
        else:
            state = get_or_create_state(session, rule.id, node_id)
            if state_cache is not None:
                state_cache[state_key] = state

        new_state = transition_state(session, state, rule, condition_met, now)

        if new_state == "firing":
            self._process_firing_notification(
                session,
                rule,
                node_id,
                labels,
                now,
                metric_context,
                state=state,
            )

    def evaluate_rule_for_node_sync(
        self,
        session: Session,
        rule_id: int,
        node_id: str,
        now: Optional[datetime] = None,
        labels: Optional[dict] = None,
        metric_context: Optional[dict] = None,
    ) -> str:
        """同步评估单条规则（用于管理接口手动触发）。

        返回评估后的状态字符串。
        """
        return evaluate_rule_for_node(
            session=session,
            rule_id=rule_id,
            node_id=node_id,
            now=now,
            cache=self._cache,
            labels=labels,
            metric_context=metric_context,
        )

    def _process_firing_notification(
        self,
        session: Session,
        rule: AlertRule,
        node_id: str,
        labels: Optional[dict],
        now: datetime,
        metric_context: Optional[dict] = None,
        *,
        state: Optional[Any] = None,
    ) -> None:
        """处理 firing 状态的通知决策与自愈触发。

        依次检查抑制、静默、去重窗口；通过则发送通知、聚合 AlertEvent/Incident，
        并在聚合成功后记录去重时间，最后调用自愈执行引擎。

        去重生效与事件/Incident 落库保持原子：仅在聚合成功后才占用去重窗口；
        聚合失败则回滚且不更新 ``last_notified_at``，保证下次 firing 能重新聚合，
        避免去重已生效但事件丢失。实际通知发送由通知渠道模块负责。
        """
        alert_labels = dict(labels or {})
        alert_labels.setdefault("node_id", node_id)
        alert_labels.setdefault("rule_id", str(rule.id))
        alert_labels.setdefault("severity", rule.severity)

        if alert_inhibition.is_inhibited(
            session, rule.id, node_id, alert_labels, now
        ):
            logger.info(
                f"告警被抑制，跳过通知: rule_id={rule.id}, node_id={node_id}"
            )
            return

        if alert_silence.is_silenced(session, alert_labels, now):
            logger.info(
                f"告警被静默，跳过通知: rule_id={rule.id}, node_id={node_id}"
            )
            return

        # 复用评估阶段已获取的 AlertState，避免重复查询；缺失时回退到查询。
        if state is None:
            state = get_or_create_state(session, rule.id, node_id)
        if not should_notify(state, now):
            logger.info(
                f"告警在去重窗口内，跳过通知: rule_id={rule.id}, node_id={node_id}"
            )
            return

        # 触发通知渠道发送（异常隔离）；失败时回滚，避免污染共享 Session。
        try:
            channel_ids = alert_rule_service._parse_channel_ids(
                rule.notification_channel_ids
            )
            notification_service.send_alert_notification(
                session=session,
                rule_id=rule.id,
                rule_name=rule.name,
                node_id=node_id,
                severity=rule.severity,
                timestamp=now,
                channel_ids=channel_ids,
                metric_context=metric_context,
            )
        except Exception as exc:  # noqa: BLE001
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning(f"触发告警通知失败: rule_id={rule.id}, error={exc}")

        # 创建 AlertEvent 并聚合到 Incident。
        # 聚合成功才占用去重窗口；失败则不更新 last_notified_at，
        # 让下一次 firing 重新聚合，杜绝“去重已生效但事件丢失”。
        agg_ok = _aggregate_incident_for_firing(
            session=session,
            rule=rule,
            node_id=node_id,
            alert_labels=alert_labels,
            now=now,
        )

        if agg_ok:
            record_notification(state, now)
            session.add(state)
            session.commit()
            logger.info(
                f"触发告警通知: rule_id={rule.id}, node_id={node_id}, "
                f"dedup_key={build_dedup_key(rule.id, node_id, alert_labels)}"
            )
        else:
            # 聚合失败：显式回滚并保持去重窗口未被占用。
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass

        # 触发自愈执行引擎，异常隔离不影响通知/聚合流程。
        # 放在最后：即使其内部吞异常使 session inactive，也不影响已提交的
        # 聚合与去重结果，会话随后由上下文管理器关闭。
        handle_alert_firing(
            session=session,
            alert_rule=rule,
            node_id=node_id,
            labels=alert_labels,
            metric_context=metric_context,
        )

    def stats(self) -> dict:
        """返回检测器统计信息。"""
        return {
            "rules_indexed": self.rules_indexed,
            "rules_skipped": self._rule_index.skipped,
            "cache": self._cache.stats(),
            "processed_events": self._processed_count,
            "error_count": self._error_count,
            "reload_failure_count": self._reload_failure_count,
            "last_latency_ms": round(self._last_latency_ms, 3),
        }

    def cleanup_cache(self) -> int:
        """清理缓存中过期条目。"""
        return self._cache.cleanup_expired()


def _aggregate_incident_for_firing(
    session: Session,
    rule: AlertRule,
    node_id: str,
    alert_labels: dict,
    now: datetime,
) -> bool:
    """为 firing 告警创建 AlertEvent 并聚合到 Incident。

    Returns:
        True  表示 AlertEvent 与 Incident 均成功落库；
        False 表示聚合阶段失败（已显式 rollback，避免会话进入 inactive），
              调用方据此决定是否占用去重窗口：失败时不更新 last_notified_at，
              保证下次 firing 能重新聚合，避免事件静默丢失。
    """
    incident = None
    alert_event = None
    try:
        alert_event = incident_service.create_alert_event(
            session=session,
            rule_id=rule.id,
            node_id=node_id,
            severity=rule.severity,
            message=rule.name,
            labels=alert_labels,
            fired_at=now,
        )
        incident = incident_service.auto_create_or_update_incident(session, alert_event)
    except Exception as exc:  # noqa: BLE001
        # 显式回滚：防止共享 Session 因异常进入 inactive，污染后续操作；
        # 同时返回 False，由调用方决定不占用去重窗口以便重聚。
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.exception(
            f"告警事件聚合失败: rule_id={rule.id}, node_id={node_id}, error={exc}"
        )
        return False

    if incident is not None:
        try:
            record_incident_event(
                session=session,
                incident_id=incident.id,
                event_type="alert",
                event_subtype="alert.firing",
                title="timeline.alert.firing.title",
                description="timeline.alert.firing.desc",
                metadata={
                    "rule_id": rule.id,
                    "rule_name": rule.name,
                    "node_id": node_id,
                    "severity": rule.severity,
                    "alert_event_id": alert_event.id,
                    "labels": alert_labels,
                },
                source="detector",
                timestamp=now,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"记录 alert.firing 时间线事件失败: "
                f"incident_id={incident.id}, error={exc}"
            )

    # 发布实时事件（异常隔离，不影响告警流程）
    try:
        publish(
            "alert.firing",
            {
                "rule_id": rule.id,
                "rule_name": rule.name,
                "node_id": node_id,
                "severity": rule.severity,
                "alert_event_id": alert_event.id,
                "incident_id": incident.id if incident is not None else None,
                "labels": alert_labels,
                "timestamp": now.isoformat().replace("+00:00", "Z"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"发布 alert.firing 事件失败: {exc}")

    return True


def _compare(value: float, op: str, threshold: float) -> bool:
    """比较数值。"""
    if op == ">":
        return value > threshold
    if op == ">=":
        return value >= threshold
    if op == "<":
        return value < threshold
    if op == "<=":
        return value <= threshold
    if op == "==":
        return value == threshold
    if op == "!=":
        return value != threshold
    return False


def _evaluate_condition(
    condition: Any,
    node_id: str,
    cache: NodeMetricCache,
) -> bool:
    """递归求值 JSON DSL 条件。"""
    try:
        op = condition.get("op")
        if op in {"AND", "OR"}:
            results = [
                _evaluate_condition(child, node_id, cache)
                for child in condition.get("conditions", [])
            ]
            if not results:
                return False
            if op == "AND":
                return all(results)
            return any(results)

        return _evaluate_leaf(condition, node_id, cache)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"条件求值异常，视为不满足: {exc}")
        return False


def _evaluate_leaf(condition: dict, node_id: str, cache: NodeMetricCache) -> bool:
    """求值叶子条件。"""
    metric_name = condition["metric"]
    op = condition["op"]
    threshold = condition["value"]
    rule_labels = condition.get("labels") or {}

    if rule_labels:
        # 指定标签：子集匹配，允许样本包含额外标签（如 node_id）
        values = cache.get_values_matching_labels(
            node_id=node_id,
            metric_name=metric_name,
            required_labels=rule_labels,
        )
    else:
        # 未指定标签：对所有时序取最大值（适用于多核 CPU 等多序列场景）
        values = cache.get_values_for_metric(node_id, metric_name)

    if not values:
        return False

    # 多值场景只要任一序列满足即视为条件满足
    return any(_compare(value, op, threshold) for value in values)


def evaluate_rule_for_node(
    session: Session,
    rule_id: int,
    node_id: str,
    now: Optional[datetime] = None,
    cache: Optional[NodeMetricCache] = None,
    labels: Optional[dict] = None,
    metric_context: Optional[dict] = None,
) -> str:
    """同步评估单条规则对指定节点的状态。

    不依赖全局检测器实例，便于管理接口与测试直接使用。
    当状态进入 firing 时，同样会执行抑制/静默/去重检查。
    """
    rule = session.get(AlertRule, rule_id)
    if rule is None:
        raise ValueError("规则不存在")
    if not rule.enabled:
        raise ValueError("规则已禁用")

    cache = cache or get_metric_cache()
    condition = parse_condition(rule.condition_type, rule.condition)
    condition_met = _evaluate_condition(condition, node_id, cache)

    state = get_or_create_state(session, rule.id, node_id)
    new_state = transition_state(
        session, state, rule, condition_met, now or datetime.now(timezone.utc)
    )

    if new_state == "firing":
        detector = get_alert_detector()
        if detector is not None:
            detector._process_firing_notification(
                session, rule, node_id, labels, now, metric_context, state=state
            )
        else:
            # 无全局检测器实例时，使用独立流程完成通知决策
            _process_firing_notification_fallback(
                session, rule, node_id, labels, now, metric_context, state=state
            )

    return new_state


def evaluate_rule_for_node_from_storage(
    session: Session,
    rule_id: int,
    node_id: str,
    now: Optional[datetime] = None,
    labels: Optional[dict] = None,
    metric_context: Optional[dict] = None,
) -> tuple[str, int]:
    """手动评估单条规则：从时序库读取最近样本评估（跨 worker 一致）。

    与 :func:`evaluate_rule_for_node` 的唯一区别是数据来源：本函数通过
    ``query_metrics`` 读取时序库最近窗口（与检测缓存 TTL 一致）内的样本，
    而非进程级 ``NodeMetricCache`` 单例。多 worker 部署下指标事件只写入
    leader 进程的 cache，非 leader worker 读 cache 恒空会导致评估恒 False
    的误判；时序库是所有 worker 共享的检测同源数据，读它才能保证一致。

    Returns:
        (state, sample_count) 元组。``sample_count == 0`` 表示评估窗口内
        无任何样本，此时**不做状态转换**（避免"无数据"被当成"条件不满足"
        而误 resolve），返回当前已持久化的状态，由调用方给出"无数据"提示。
    """
    rule = session.get(AlertRule, rule_id)
    if rule is None:
        raise ValueError("规则不存在")
    if not rule.enabled:
        raise ValueError("规则已禁用")

    now = now or datetime.now(timezone.utc)
    window_seconds = settings.ALERT_DETECTOR_CACHE_TTL_SECONDS
    start = now - timedelta(seconds=window_seconds)

    condition = parse_condition(rule.condition_type, rule.condition)
    metric_names = extract_metric_names(condition)

    # 一次性缓存：复用 NodeMetricCache 的"每序列取最新值 + 标签子集匹配"语义，
    # 与实时检测的求值口径完全一致。
    recent_cache = NodeMetricCache(ttl_seconds=window_seconds)
    sample_count = 0
    for name in sorted(metric_names):
        samples = query_metrics(
            node_id=node_id,
            metric_name=name,
            start=start,
            end=now,
        )
        for sample in samples:
            recent_cache.update(
                node_id=node_id,
                metric_name=name,
                labels=sample.labels,
                value=sample.value,
                timestamp=sample.timestamp,
            )
        sample_count += len(samples)

    if sample_count == 0:
        # 无最近数据：只读当前状态，不落库、不转换，避免误 resolve。
        existing = session.exec(
            select(AlertState).where(
                AlertState.rule_id == rule_id,
                AlertState.node_id == node_id,
            )
        ).first()
        return (existing.state if existing is not None else "idle"), 0

    new_state = evaluate_rule_for_node(
        session=session,
        rule_id=rule_id,
        node_id=node_id,
        now=now,
        cache=recent_cache,
        labels=labels,
        metric_context=metric_context,
    )
    return new_state, sample_count


def _process_firing_notification_fallback(
    session: Session,
    rule: AlertRule,
    node_id: str,
    labels: Optional[dict],
    now: Optional[datetime],
    metric_context: Optional[dict] = None,
    *,
    state: Optional[Any] = None,
) -> None:
    """无全局检测器实例时的 firing 通知决策回退实现。

    与 :meth:`AlertDetector._process_firing_notification` 保持相同的去重/聚合原子
    语义：聚合成功才占用去重窗口，聚合失败则回滚并允许下次 firing 重聚。
    """
    now = now or datetime.now(timezone.utc)
    alert_labels = dict(labels or {})
    alert_labels.setdefault("node_id", node_id)
    alert_labels.setdefault("rule_id", str(rule.id))
    alert_labels.setdefault("severity", rule.severity)

    if alert_inhibition.is_inhibited(session, rule.id, node_id, alert_labels, now):
        logger.info(
            f"告警被抑制，跳过通知: rule_id={rule.id}, node_id={node_id}"
        )
        return

    if alert_silence.is_silenced(session, alert_labels, now):
        logger.info(
            f"告警被静默，跳过通知: rule_id={rule.id}, node_id={node_id}"
        )
        return

    # 复用调用方已获取的 AlertState，避免重复查询；缺失时回退到查询。
    if state is None:
        state = get_or_create_state(session, rule.id, node_id)
    if not should_notify(state, now):
        logger.info(
            f"告警在去重窗口内，跳过通知: rule_id={rule.id}, node_id={node_id}"
        )
        return

    # 触发通知渠道发送（异常隔离）；失败时回滚，避免污染 Session。
    try:
        channel_ids = alert_rule_service._parse_channel_ids(
            rule.notification_channel_ids
        )
        notification_service.send_alert_notification(
            session=session,
            rule_id=rule.id,
            rule_name=rule.name,
            node_id=node_id,
            severity=rule.severity,
            timestamp=now,
            channel_ids=channel_ids,
            metric_context=metric_context,
        )
    except Exception as exc:  # noqa: BLE001
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning(f"触发告警通知失败: rule_id={rule.id}, error={exc}")

    # 创建 AlertEvent 并聚合到 Incident；聚合成功才占用去重窗口。
    agg_ok = _aggregate_incident_for_firing(
        session=session,
        rule=rule,
        node_id=node_id,
        alert_labels=alert_labels,
        now=now,
    )

    if agg_ok:
        record_notification(state, now)
        session.add(state)
        session.commit()
        logger.info(
            f"触发告警通知: rule_id={rule.id}, node_id={node_id}, "
            f"dedup_key={build_dedup_key(rule.id, node_id, alert_labels)}"
        )
    else:
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass

    # 触发自愈执行引擎，异常隔离不影响通知/聚合流程。
    handle_alert_firing(
        session=session,
        alert_rule=rule,
        node_id=node_id,
        labels=alert_labels,
        metric_context=metric_context,
    )


# 全局单例检测器
_detector: Optional[AlertDetector] = None
_detector_lock = threading.Lock()


def init_alert_detector(
    engine: Engine,
    cache: Optional[NodeMetricCache] = None,
    executor: Optional[ThreadPoolExecutor] = None,
) -> AlertDetector:
    """初始化并启动全局告警检测引擎。"""
    global _detector
    if _detector is None:
        with _detector_lock:
            if _detector is None:
                _detector = AlertDetector(engine, cache, executor)
                _detector.start()
                # 订阅规则变更事件，自动重建索引
                from app.services.alert_rule import on_rule_changed

                on_rule_changed(_detector.reload_rules)
    return _detector


def get_alert_detector() -> Optional[AlertDetector]:
    """获取全局告警检测引擎实例。"""
    return _detector


def stop_alert_detector() -> None:
    """关闭全局告警检测引擎。"""
    global _detector
    if _detector is not None:
        # 取消规则变更回调，避免关闭后仍被调用
        off_rule_changed(_detector.reload_rules)
        _detector.stop()
        _detector = None
