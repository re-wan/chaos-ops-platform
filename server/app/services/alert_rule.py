"""告警规则业务逻辑。

包含规则校验、JSON DSL 解析、简化 PromQL 转 JSON DSL、License 数量上限预留。
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlmodel import Session, select

from app.core.licensing import get_max_alert_rules
from app.core.logger import get_logger
from app.models.alert_rule import AlertRule
from app.models.heal_rule import HealRule
from app.models.node_group import NodeGroup
from app.models.optimization_suggestion import OptimizationSuggestion
from app.schemas.alert_rule import VALID_COMPARISON_OPS
from app.services.metric_query import get_metric_names

logger = get_logger("services.alert_rule")

VALID_SCOPES = {"global", "node", "node_group"}

# 规则变更回调注册表，供告警检测引擎等订阅
_rule_change_callbacks: list[Callable[[], None]] = []


def on_rule_changed(callback: Callable[[], None]) -> None:
    """注册规则变更回调。"""
    _rule_change_callbacks.append(callback)


def off_rule_changed(callback: Callable[[], None]) -> None:
    """取消注册规则变更回调。"""
    if callback in _rule_change_callbacks:
        _rule_change_callbacks.remove(callback)


def reset_rule_change_callbacks() -> None:
    """清空所有规则变更回调（仅用于测试隔离）。"""
    _rule_change_callbacks.clear()


def _notify_rule_changed() -> None:
    """通知所有订阅者规则已变更。"""
    for callback in _rule_change_callbacks:
        try:
            callback()
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"规则变更回调失败: {exc}")
VALID_CONDITION_TYPES = {"json_dsl", "promql"}
VALID_SEVERITIES = {"critical", "warning", "info"}

# Phase 1 预留：免费版规则数量上限。Phase 2 由 License 模块替换。
_FREE_TIER_RULE_LIMIT = 5

# 简化 PromQL 词法规则（大小写不敏感，通过编译标志控制）
_TOKEN_SPEC = [
    ("OR", r"\bor\b"),
    ("AND", r"\band\b"),
    ("OP", r">=|<=|==|!=|>|<"),
    ("EQ", r"="),
    ("NUMBER", r"\d+(\.\d*)?|\.\d+"),
    ("STRING", r'"([^"\\]|\\.)*"'),
    ("LBRACE", r"\{"),
    ("RBRACE", r"\}"),
    ("COMMA", r","),
    ("IDENT", r"[a-zA-Z_:][a-zA-Z0-9_:]*"),
    ("SKIP", r"[ \t\n\r]+"),
    ("MISMATCH", r"."),
]
_TOKEN_REGEX = "|".join(
    f"(?P<{name}>{pattern})" for name, pattern in _TOKEN_SPEC
)
_TOKEN_PATTERN = re.compile(_TOKEN_REGEX, re.IGNORECASE)


def _serialize_channel_ids(channel_ids: Optional[list[int]]) -> str:
    """将通知渠道 ID 列表序列化为 JSON 字符串。"""
    if channel_ids is None:
        return "[]"
    return json.dumps(sorted(set(channel_ids)), ensure_ascii=False)


def _parse_channel_ids(channel_ids_str: Optional[str]) -> list[int]:
    """将 JSON 字符串解析为通知渠道 ID 列表。"""
    if not channel_ids_str:
        return []
    try:
        data = json.loads(channel_ids_str)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [int(x) for x in data if isinstance(x, int)]


def validate_rule_name(name: str) -> None:
    """校验规则名称是否合法。"""
    if not name or len(name) < 1 or len(name) > 128:
        raise ValueError("规则名称长度必须在 1-128 字符之间")


def validate_scope(scope: str, scope_target: Optional[str]) -> None:
    """校验规则范围与目标是否匹配。"""
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope 必须是 {VALID_SCOPES} 之一")
    if scope in {"node", "node_group"} and not scope_target:
        raise ValueError(f"scope={scope} 时必须提供 scope_target")
    if scope == "global" and scope_target:
        raise ValueError("scope=global 时 scope_target 必须为空")


def _validate_node_group_exists(
    db: Session, scope: str, scope_target: Optional[str]
) -> None:
    """校验 node_group scope 指向的节点组存在且 ID 合法。"""
    if scope != "node_group" or not scope_target:
        return
    try:
        group_id = int(scope_target)
    except (TypeError, ValueError):
        raise ValueError("node_group scope 的 scope_target 必须是节点组 ID")

    group = db.get(NodeGroup, group_id)
    if group is None:
        raise ValueError(f"节点组不存在: group_id={group_id}")


def validate_severity(severity: str) -> None:
    """校验严重度是否合法。"""
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"severity 必须是 {VALID_SEVERITIES} 之一")


def validate_condition_type(condition_type: str) -> None:
    """校验条件语法类型是否合法。"""
    if condition_type not in VALID_CONDITION_TYPES:
        raise ValueError(f"condition_type 必须是 {VALID_CONDITION_TYPES} 之一")


def validate_durations(pending: int, resolve: int) -> None:
    """校验 pending/resolve 持续时间。"""
    if pending < 0:
        raise ValueError("pending_duration_seconds 必须 >= 0")
    if resolve < 0:
        raise ValueError("resolve_duration_seconds 必须 >= 0")


def _validate_leaf_condition(condition: Any) -> dict:
    """校验叶子条件对象并返回规范化结果。"""
    if not isinstance(condition, dict):
        raise ValueError("告警条件叶子节点必须是 JSON 对象")

    metric = condition.get("metric")
    op = condition.get("op")
    value = condition.get("value")
    labels = condition.get("labels")

    if not isinstance(metric, str) or not metric:
        raise ValueError("condition.metric 必须是有效字符串")
    if op not in VALID_COMPARISON_OPS:
        raise ValueError(
            f"condition.op 必须是 {sorted(VALID_COMPARISON_OPS)} 之一"
        )
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("condition.value 必须是数值")
    if labels is not None and not isinstance(labels, dict):
        raise ValueError("condition.labels 必须是 JSON 对象")

    return {
        "metric": metric,
        "op": op,
        "value": value,
        "labels": labels if labels else None,
    }


def _validate_condition_node(node: Any) -> dict:
    """递归校验 JSON DSL 条件节点。"""
    if not isinstance(node, dict):
        raise ValueError("告警条件必须是 JSON 对象")

    op = node.get("op")
    if op in {"AND", "OR"}:
        conditions = node.get("conditions")
        if not isinstance(conditions, list) or len(conditions) < 2:
            raise ValueError(f"逻辑节点 '{op}' 必须包含至少两个子条件")
        return {
            "op": op,
            "conditions": [_validate_condition_node(c) for c in conditions],
        }

    return _validate_leaf_condition(node)


def parse_json_dsl(condition_str: str) -> dict:
    """解析并校验 JSON DSL 条件字符串。"""
    try:
        data = json.loads(condition_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"condition 不是合法 JSON: {e}")

    return _validate_condition_node(data)


def _tokenize(text: str) -> list[tuple[str, str]]:
    """将简化 PromQL 字符串切分为 Token 列表。"""
    tokens = []
    for match in _TOKEN_PATTERN.finditer(text):
        kind = match.lastgroup
        value = match.group()
        if kind == "SKIP":
            continue
        if kind == "MISMATCH":
            raise ValueError(f"PromQL 包含非法字符: {value!r}")
        tokens.append((kind, value))
    return tokens


def _parse_number(text: str) -> float | int:
    """将数字字符串解析为 int 或 float。"""
    if "." in text:
        return float(text)
    return int(text)


def _unquote(text: str) -> str:
    """去掉字符串字面量两端的引号并处理转义。"""
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        inner = text[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return text


class _PromQLParser:
    """简化 PromQL 递归下降解析器。

    支持的语法：
        expr     := or_expr
        or_expr  := and_expr ("or" and_expr)*
        and_expr := comparison ("and" comparison)*
        comparison := metric labels? op number
        labels   := "{" label_pair ("," label_pair)* "}"
        label_pair := ident "==" string
    """

    def __init__(self, text: str):
        self.tokens = _tokenize(text)
        self.pos = 0

    def _current(self) -> tuple[str, str]:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return ("EOF", "")

    def _eat(self, kind: str) -> tuple[str, str]:
        token = self._current()
        if token[0] == kind:
            self.pos += 1
            return token
        raise ValueError(f"预期 {kind}，得到 {token[0]}: {token[1]!r}")

    def parse(self) -> dict:
        """解析完整表达式并返回 JSON DSL。"""
        result = self._parse_or()
        if self._current()[0] != "EOF":
            raise ValueError(
                f"PromQL 解析未结束，剩余 token: {self._current()[1]!r}"
            )
        return result

    def _parse_or(self) -> dict:
        left = self._parse_and()
        while self._current()[0] == "OR":
            self._eat("OR")
            right = self._parse_and()
            left = {"op": "OR", "conditions": [left, right]}
        return left

    def _parse_and(self) -> dict:
        left = self._parse_comparison()
        while self._current()[0] == "AND":
            self._eat("AND")
            right = self._parse_comparison()
            left = {"op": "AND", "conditions": [left, right]}
        return left

    def _parse_comparison(self) -> dict:
        metric_tok = self._eat("IDENT")
        metric = metric_tok[1]

        labels: dict[str, str] = {}
        if self._current()[0] == "LBRACE":
            self._eat("LBRACE")
            labels = self._parse_labels()
            self._eat("RBRACE")

        op_tok = self._eat("OP")
        value_tok = self._eat("NUMBER")

        return {
            "metric": metric,
            "op": op_tok[1],
            "value": _parse_number(value_tok[1]),
            "labels": labels if labels else None,
        }

    def _parse_labels(self) -> dict[str, str]:
        labels: dict[str, str] = {}
        while True:
            key_tok = self._eat("IDENT")
            op_tok = self._current()
            if op_tok[0] != "EQ":
                raise ValueError(
                    "PromQL 标签匹配只支持 '='，"
                    f"得到 {op_tok[0]}: {op_tok[1]!r}"
                )
            self._eat("EQ")
            val_tok = self._eat("STRING")
            labels[key_tok[1]] = _unquote(val_tok[1])

            if self._current()[0] == "COMMA":
                self._eat("COMMA")
                continue
            break
        return labels


def parse_promql(condition: str) -> dict:
    """将简化 PromQL 表达式解析为内部 JSON DSL。"""
    parser = _PromQLParser(condition)
    dsl = parser.parse()
    # 复用 JSON DSL 校验确保操作符等符合规范
    return _validate_condition_node(dsl)


def parse_condition(condition_type: str, condition: str) -> dict:
    """根据 condition_type 解析条件字符串。"""
    validate_condition_type(condition_type)
    if condition_type == "json_dsl":
        return parse_json_dsl(condition)
    return parse_promql(condition)


def extract_metric_names(condition_node: Any) -> set[str]:
    """从已解析的 JSON DSL 中提取所有指标名。"""
    names: set[str] = set()
    if isinstance(condition_node, dict) and condition_node.get("op") in {
        "AND",
        "OR",
    }:
        for child in condition_node.get("conditions", []):
            names.update(extract_metric_names(child))
    elif isinstance(condition_node, dict):
        metric = condition_node.get("metric")
        if isinstance(metric, str):
            names.add(metric)
    return names


def _warn_unknown_metrics(
    metric_names: set[str],
    scope: str,
    scope_target: Optional[str],
) -> None:
    """对未上报的指标名给出警告，但不阻止规则创建。"""
    if scope != "node" or not scope_target:
        return

    try:
        known_names = set(get_metric_names(node_id=scope_target))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"规则引用指标校验失败 (scope_target={scope_target}): {exc}"
        )
        return

    unknown = metric_names - known_names
    if unknown:
        logger.warning(
            f"规则引用指标当前未上报: {sorted(unknown)}; "
            f"scope_target={scope_target}"
        )


def check_license_rule_limit(db: Session) -> None:
    """校验 License 告警规则数量上限。"""
    limit = get_max_alert_rules()
    if limit < 0:
        return
    count = len(db.exec(select(AlertRule)).all())
    if count >= limit:
        raise ValueError(
            f"当前 License 最多允许 {limit} 条告警规则，"
            "请升级 License 后继续使用"
        )


def create_alert_rule(
    db: Session,
    name: str,
    description: Optional[str],
    scope: str,
    scope_target: Optional[str],
    condition_type: str,
    condition: str,
    pending_duration_seconds: int,
    resolve_duration_seconds: int,
    severity: str,
    enabled: bool,
    notification_channel_ids: list[int],
) -> AlertRule:
    """创建告警规则。"""
    validate_rule_name(name)
    validate_scope(scope, scope_target)
    _validate_node_group_exists(db, scope, scope_target)
    validate_severity(severity)
    validate_durations(pending_duration_seconds, resolve_duration_seconds)

    existing = db.exec(
        select(AlertRule).where(AlertRule.name == name)
    ).first()
    if existing is not None:
        raise ValueError("规则名称已存在")

    check_license_rule_limit(db)

    parsed_condition = parse_condition(condition_type, condition)
    _warn_unknown_metrics(
        extract_metric_names(parsed_condition),
        scope,
        scope_target,
    )

    rule = AlertRule(
        name=name,
        description=description,
        scope=scope,
        scope_target=scope_target,
        condition_type=condition_type,
        condition=condition,
        pending_duration_seconds=pending_duration_seconds,
        resolve_duration_seconds=resolve_duration_seconds,
        severity=severity,
        enabled=enabled,
        notification_channel_ids=_serialize_channel_ids(notification_channel_ids),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)

    logger.info(f"创建告警规则: id={rule.id}, name={rule.name}, scope={rule.scope}")
    _notify_rule_changed()
    return rule


def update_alert_rule(
    db: Session,
    rule: AlertRule,
    name: Optional[str] = None,
    description: Optional[str] = None,
    scope: Optional[str] = None,
    scope_target: Optional[str] = None,
    condition_type: Optional[str] = None,
    condition: Optional[str] = None,
    pending_duration_seconds: Optional[int] = None,
    resolve_duration_seconds: Optional[int] = None,
    severity: Optional[str] = None,
    enabled: Optional[bool] = None,
    notification_channel_ids: Optional[list[int]] = None,
) -> AlertRule:
    """更新告警规则。"""
    new_scope = scope if scope is not None else rule.scope
    new_scope_target = (
        scope_target if scope_target is not None else rule.scope_target
    )

    validate_scope(new_scope, new_scope_target)
    _validate_node_group_exists(db, new_scope, new_scope_target)

    if name is not None and name != rule.name:
        validate_rule_name(name)
        existing = db.exec(
            select(AlertRule).where(
                AlertRule.name == name,
                AlertRule.id != rule.id,
            )
        ).first()
        if existing is not None:
            raise ValueError("规则名称已存在")
        rule.name = name

    if description is not None:
        rule.description = description
    if scope is not None:
        rule.scope = scope
    if scope_target is not None:
        rule.scope_target = scope_target

    new_condition_type = (
        condition_type if condition_type is not None else rule.condition_type
    )
    new_condition = condition if condition is not None else rule.condition

    if condition_type is not None or condition is not None:
        parse_condition(new_condition_type, new_condition)
        rule.condition_type = new_condition_type
        rule.condition = new_condition

    if pending_duration_seconds is not None:
        rule.pending_duration_seconds = pending_duration_seconds
    if resolve_duration_seconds is not None:
        rule.resolve_duration_seconds = resolve_duration_seconds
    if severity is not None:
        validate_severity(severity)
        rule.severity = severity
    if enabled is not None:
        rule.enabled = enabled
    if notification_channel_ids is not None:
        rule.notification_channel_ids = _serialize_channel_ids(
            notification_channel_ids
        )

    validate_durations(
        rule.pending_duration_seconds,
        rule.resolve_duration_seconds,
    )

    # 重新解析并警告未知指标
    parsed_condition = parse_condition(rule.condition_type, rule.condition)
    _warn_unknown_metrics(
        extract_metric_names(parsed_condition),
        rule.scope,
        rule.scope_target,
    )

    rule.updated_at = datetime.now(timezone.utc)
    db.add(rule)
    db.commit()
    db.refresh(rule)

    logger.info(f"更新告警规则: id={rule.id}, name={rule.name}")
    _notify_rule_changed()
    return rule


def delete_alert_rule(db: Session, rule: AlertRule) -> None:
    """删除告警规则。

    删除前先处理两类按 id 的引用，杜绝悬挂行在 id 复用时错绑到新建规则：

    1. ``HealRule.alert_rule_id`` —— 自愈规则会直接驱动修复执行，错绑危害最大，
       故**拒绝删除**并提示先删除/解绑自愈规则（API 层映射为 409）。
    2. ``OptimizationSuggestion.target_id == "alert_rule:N"`` 且仍处于可 apply 的
       pending 状态 —— 建议是分析引擎定期再生的草稿，**同事务级联删除**，
       避免对已删规则误 apply；applied/rejected/rolled_back 为历史记录，保留。

    规则本身被硬删除；未来 alert_history 表通过外键 ON DELETE SET NULL
    保留历史告警记录，不在此处级联删除。AlertRule 主键已开启
    sqlite_autoincrement，删除后 id 不复用，与上述清理形成双重保险。
    """
    heal_refs = db.exec(
        select(HealRule).where(HealRule.alert_rule_id == rule.id)
    ).all()
    if heal_refs:
        ids = ", ".join(str(r.id) for r in heal_refs)
        raise ValueError(
            f"告警规则仍被 {len(heal_refs)} 条自愈规则引用"
            f"（heal_rule id: {ids}），请先删除或解绑对应自愈规则后再删除告警规则"
        )

    stale_suggestions = db.exec(
        select(OptimizationSuggestion).where(
            OptimizationSuggestion.target_id == f"alert_rule:{rule.id}",
            OptimizationSuggestion.status == "pending",
        )
    ).all()
    for suggestion in stale_suggestions:
        db.delete(suggestion)

    db.delete(rule)
    db.commit()
    logger.info(
        f"删除告警规则: id={rule.id}, name={rule.name}, "
        f"级联清理 pending 优化建议 {len(stale_suggestions)} 条"
    )
    _notify_rule_changed()


def toggle_alert_rule(db: Session, rule: AlertRule) -> AlertRule:
    """切换规则启用/禁用状态。"""
    rule.enabled = not rule.enabled
    rule.updated_at = datetime.now(timezone.utc)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    logger.info(
        f"切换告警规则状态: id={rule.id}, name={rule.name}, "
        f"enabled={rule.enabled}"
    )
    return rule
