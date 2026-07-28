"""节点最近指标值缓存。

为告警检测引擎提供每个节点、每个指标、每组标签的最新值，
支持 TTL 过期与容量上限，避免内存无限增长。
"""

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("core.metric_cache")


@dataclass
class _MetricValue:
    value: float
    timestamp: datetime


class NodeMetricCache:
    """节点指标最近值缓存。

    结构：node_id -> metric_name -> labels_hash -> _MetricValue
    """

    def __init__(
        self,
        ttl_seconds: Optional[int] = None,
        max_entries: Optional[int] = None,
    ):
        self._ttl_seconds = ttl_seconds or settings.ALERT_DETECTOR_CACHE_TTL_SECONDS
        self._max_entries = max_entries or settings.ALERT_DETECTOR_CACHE_MAX_ENTRIES
        self._cache: dict[str, dict[str, OrderedDict[str, _MetricValue]]] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _hash_labels(labels: Optional[dict]) -> str:
        """将标签字典序列化为稳定哈希键。"""
        if not labels:
            return "__empty__"
        return json.dumps(labels, sort_keys=True, ensure_ascii=False)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def update(
        self,
        node_id: str,
        metric_name: str,
        labels: Optional[dict],
        value: float,
        timestamp: datetime,
    ) -> None:
        """更新缓存中的最新值。"""
        labels_hash = self._hash_labels(labels)
        with self._lock:
            node_cache = self._cache.setdefault(node_id, {})
            metric_cache = node_cache.setdefault(metric_name, OrderedDict())
            # 移动到末尾表示最新访问
            metric_cache.pop(labels_hash, None)
            metric_cache[labels_hash] = _MetricValue(value, timestamp)
            self._enforce_size_limit()

    def get_value(
        self,
        node_id: str,
        metric_name: str,
        labels: Optional[dict] = None,
    ) -> Optional[float]:
        """获取指定节点、指标、标签组合的最新值（未过期）。"""
        labels_hash = self._hash_labels(labels)
        with self._lock:
            node_cache = self._cache.get(node_id)
            if node_cache is None:
                self._misses += 1
                return None
            metric_cache = node_cache.get(metric_name)
            if metric_cache is None:
                self._misses += 1
                return None
            entry = metric_cache.get(labels_hash)
            if entry is None:
                self._misses += 1
                return None
            if self._is_expired(entry):
                del metric_cache[labels_hash]
                self._misses += 1
                return None
            # 更新访问顺序
            metric_cache.move_to_end(labels_hash)
            self._hits += 1
            return entry.value

    def get_values_for_metric(
        self,
        node_id: str,
        metric_name: str,
    ) -> list[float]:
        """获取某节点某指标下所有未过期的值列表。"""
        values: list[float] = []
        with self._lock:
            node_cache = self._cache.get(node_id)
            if node_cache is None:
                self._misses += 1
                return values
            metric_cache = node_cache.get(metric_name)
            if metric_cache is None:
                self._misses += 1
                return values

            expired_keys: list[str] = []
            for labels_hash, entry in metric_cache.items():
                if self._is_expired(entry):
                    expired_keys.append(labels_hash)
                else:
                    values.append(entry.value)
            for key in expired_keys:
                del metric_cache[key]

            if not values:
                self._misses += 1
            else:
                self._hits += 1
            return values

    def get_values_matching_labels(
        self,
        node_id: str,
        metric_name: str,
        required_labels: dict,
    ) -> list[float]:
        """获取标签包含所有 required_labels 的未过期值列表（子集匹配）。"""
        values: list[float] = []
        with self._lock:
            node_cache = self._cache.get(node_id)
            if node_cache is None:
                self._misses += 1
                return values
            metric_cache = node_cache.get(metric_name)
            if metric_cache is None:
                self._misses += 1
                return values

            expired_keys: list[str] = []
            for labels_hash, entry in metric_cache.items():
                if self._is_expired(entry):
                    expired_keys.append(labels_hash)
                    continue
                labels = (
                    json.loads(labels_hash)
                    if labels_hash != "__empty__"
                    else {}
                )
                if all(
                    labels.get(key) == value
                    for key, value in required_labels.items()
                ):
                    values.append(entry.value)
            for key in expired_keys:
                del metric_cache[key]

            if not values:
                self._misses += 1
            else:
                self._hits += 1
            return values

    def _is_expired(self, entry: _MetricValue) -> bool:
        age = (self._now() - entry.timestamp).total_seconds()
        return age > self._ttl_seconds

    def _enforce_size_limit(self) -> None:
        """按 LRU 策略淘汰旧条目，控制缓存总条目数。"""
        total = sum(
            len(metric_cache)
            for node_cache in self._cache.values()
            for metric_cache in node_cache.values()
        )
        if total <= self._max_entries:
            return

        # 收集所有条目，按写入时间排序，淘汰最旧的
        all_entries: list[tuple[str, str, str, datetime]] = []
        for node_id, node_cache in self._cache.items():
            for metric_name, metric_cache in node_cache.items():
                for labels_hash, entry in metric_cache.items():
                    all_entries.append((node_id, metric_name, labels_hash, entry.timestamp))

        all_entries.sort(key=lambda x: x[3])
        to_evict = total - self._max_entries
        for node_id, metric_name, labels_hash, _ in all_entries[:to_evict]:
            metric_cache = self._cache.get(node_id, {}).get(metric_name)
            if metric_cache and labels_hash in metric_cache:
                del metric_cache[labels_hash]

    def cleanup_expired(self) -> int:
        """清理所有过期条目，返回清理数量。"""
        removed = 0
        with self._lock:
            for node_id in list(self._cache.keys()):
                node_cache = self._cache[node_id]
                for metric_name in list(node_cache.keys()):
                    metric_cache = node_cache[metric_name]
                    expired = [
                        labels_hash
                        for labels_hash, entry in metric_cache.items()
                        if self._is_expired(entry)
                    ]
                    for labels_hash in expired:
                        del metric_cache[labels_hash]
                        removed += 1
                    if not metric_cache:
                        del node_cache[metric_name]
                if not node_cache:
                    del self._cache[node_id]
        return removed

    def stats(self) -> dict:
        """返回缓存统计信息。"""
        with self._lock:
            total_nodes = len(self._cache)
            total_metrics = sum(len(node_cache) for node_cache in self._cache.values())
            total_entries = sum(
                len(metric_cache)
                for node_cache in self._cache.values()
                for metric_cache in node_cache.values()
            )
            return {
                "nodes": total_nodes,
                "metrics": total_metrics,
                "entries": total_entries,
                "hits": self._hits,
                "misses": self._misses,
                "ttl_seconds": self._ttl_seconds,
                "max_entries": self._max_entries,
            }

    def clear(self) -> None:
        """清空缓存（主要用于测试）。"""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0


# 全局单例缓存
_metric_cache: Optional[NodeMetricCache] = None
_cache_lock = threading.Lock()


def get_metric_cache(
    ttl_seconds: Optional[int] = None,
    max_entries: Optional[int] = None,
) -> NodeMetricCache:
    """获取全局指标缓存实例。"""
    global _metric_cache
    if _metric_cache is None:
        with _cache_lock:
            if _metric_cache is None:
                _metric_cache = NodeMetricCache(
                    ttl_seconds=ttl_seconds,
                    max_entries=max_entries,
                )
    return _metric_cache
