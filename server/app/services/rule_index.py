"""告警规则倒排索引与条件预编译（Phase 3 Step 05，对齐 PERFORMANCE_DESIGN §4.2）。

问题：规模化场景下规则可达 5000+，若每条指标到达都遍历全部规则求值，
检测延迟会随规则数线性增长，成为热点。

方案：

- **倒排索引**：启动 / 规则变更时按 ``metric_name`` 建立 ``metric -> {rule_id}``
  的倒排索引，指标到达时只评估命中指标名的少量规则，避免全量扫描。
- **条件预编译**：在 ``rebuild`` 时对每条规则的 ``condition`` 调用一次
  ``parse_condition``（含 JSON DSL / PromQL 解析与校验），缓存解析后的内部表达式。
  热路径评估直接复用缓存，避免每次指标事件重复解析 DSL。

一致性：索引随规则 CRUD 通过 ``reload_rules`` 全量重建（复用既有
``on_rule_changed`` 发码点与 ``reload_detector_rules_task`` 定时刷新），
保证不漏告警、不误告警。重建采用"先构造新索引、再加锁整体替换"的方式，
读侧无锁、切换原子，评估期间不会读到半成品的索引；构建过程中任何异常
（含规则集合迭代中途失败）都在替换之前抛出，旧索引继续服役。

可观测性：单条规则解析/提取失败时跳过该规则并计数（``skipped``），
逐条日志含 rule_id 与原因；重建结束后若存在跳过，再以 ERROR 级输出汇总
（数量 + rule_id 列表），便于日志告警系统捕获"规则失效导致漏告警"。

线程安全：写（rebuild）与读（rules_for / get_parsed）通过 ``RLock`` 保护；
读操作返回副本，调用方修改不影响内部状态。
"""

from __future__ import annotations

import threading
from typing import Iterable, Optional

from app.core.logger import get_logger
from app.models.alert_rule import AlertRule
from app.services.alert_rule import extract_metric_names, parse_condition

logger = get_logger("services.rule_index")


class RuleIndex:
    """按 metric_name 的告警规则倒排索引 + 预编译条件缓存。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # metric_name -> {rule_id}
        self._metric_to_rules: dict[str, set[int]] = {}
        # rule_id -> 预编译后的条件内部表达式
        self._parsed_conditions: dict[int, dict] = {}
        # 统计：上次重建时跳过的规则数（解析失败）
        self._skipped: int = 0

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def rebuild(self, rules: Iterable[AlertRule]) -> None:
        """根据规则集合全量重建索引（原子替换）。

        先在局部变量完整构建新索引，最后加锁整体替换：构建期间抛出任何
        异常（含 ``rules`` 迭代中途失败）都不会污染在役索引——旧索引继续
        服务检测，由调用方负责告警。
        """
        new_index: dict[str, set[int]] = {}
        new_parsed: dict[int, dict] = {}
        skipped = 0
        skipped_rule_ids: list[int] = []

        for rule in rules:
            if rule.id is None:
                continue
            try:
                # 解析与指标名提取放在同一 try 内：单条规则的任意失败只跳过
                # 该规则本身，不中断其余规则的索引构建。
                condition = parse_condition(rule.condition_type, rule.condition)
                metric_names = extract_metric_names(condition)
                if not metric_names:
                    # 无指标名的规则（理论上不会通过校验），保守跳过。
                    raise ValueError("规则无可用指标名")
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                skipped_rule_ids.append(rule.id)
                logger.warning(
                    f"规则条件解析失败，跳过索引（该规则暂不参与检测）: "
                    f"rule_id={rule.id}, error={exc}"
                )
                continue

            new_parsed[rule.id] = condition
            for name in metric_names:
                new_index.setdefault(name, set()).add(rule.id)

        with self._lock:
            self._metric_to_rules = new_index
            self._parsed_conditions = new_parsed
            self._skipped = skipped

        if skipped:
            # ERROR 级汇总：规则被跳过 = 漏告警风险，需可被日志告警捕获。
            logger.error(
                f"规则索引重建完成，但 {skipped} 条规则解析失败被跳过"
                f"（这些规则当前不产生任何告警）: rule_ids={skipped_rule_ids}"
            )

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def rules_for(self, metric_name: str) -> set[int]:
        """返回关心指定指标名的规则 ID 集合（副本）。"""
        with self._lock:
            return set(self._metric_to_rules.get(metric_name, ()))

    def get_parsed(self, rule_id: int) -> Optional[dict]:
        """返回规则预编译后的条件内部表达式；不存在返回 None。"""
        with self._lock:
            return self._parsed_conditions.get(rule_id)

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #
    @property
    def rule_count(self) -> int:
        """当前索引中的规则数量（去重）。"""
        with self._lock:
            ids: set[int] = set()
            for s in self._metric_to_rules.values():
                ids.update(s)
            return len(ids)

    @property
    def metric_count(self) -> int:
        """当前索引覆盖的指标名数量。"""
        with self._lock:
            return len(self._metric_to_rules)

    @property
    def skipped(self) -> int:
        """上次重建时跳过的规则数。"""
        with self._lock:
            return self._skipped

    def clear(self) -> None:
        """清空索引（测试隔离用）。"""
        with self._lock:
            self._metric_to_rules = {}
            self._parsed_conditions = {}
            self._skipped = 0
