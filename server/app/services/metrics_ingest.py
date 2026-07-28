"""指标摄入服务。

负责将 Agent 上报的指标样本通过 MetricBackend 抽象写入时序数据库。
具体后端（InfluxDB / SQLite / Mock）由 `get_metric_backend()` 决定，
支持运行时切换，便于运维与测试。
"""

import re
from datetime import datetime, timezone
from typing import Optional

from app.core.events import MetricIngestedEvent, publish_metric_ingested
from app.core.logger import get_logger
from app.core.metric_backend import MetricBackend
from app.core.metric_influxdb import InfluxDBMetricBackend
from app.core.metric_sqlite import SQLiteMetricBackend
from app.core.queues import METRIC_WRITE_BATCH_MAX, QUEUE_METRIC_WRITE
from app.core.system_settings import get_metric_backend_type, set_metric_backend_type
from app.models.metric import MetricSample
from sqlmodel import Session

logger = get_logger("services.metrics_ingest")

# 查询字符串值白名单：仅允许安全字符，从源头阻止 InfluxDB Flux 注入。
# 覆盖合法 node_id / metric_name / label value 命名（含 . _ : -），
# 显式拒绝引号、反斜杠、空白、正则元字符等，避免越读跨节点/跨 measurement 数据或
# 正则 DoS（如恶意 node_id="x\" or r._measurement=~/.*/"）。
# 注：SQLite 后端使用参数化查询不受影响；此校验主要保护 InfluxDB 后端。
_QUERY_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _validate_query_value(name: str, value: object) -> None:
    """校验查询字符串值符合白名单，非法值抛 ValueError（由 API 层统一转 400）。

    用于 ``query_metrics`` 入口统一校验 node_id / metric_name / label value，
    确保开放 API（/api/open/v1/metrics）与 Web 指标接口都不会把未转义的用户输入
    拼进 Flux。错误信息不回显具体值，避免泄露注入探测结果。
    """
    if value is None:
        return
    text = value if isinstance(value, str) else str(value)
    if not _QUERY_VALUE_PATTERN.match(text):
        raise ValueError(
            f"查询参数 {name} 含非法字符，仅允许字母、数字、下划线、点、冒号、短横线"
        )


# 全局单例后端实例（懒加载）
_backend: Optional[MetricBackend] = None


def _create_backend(backend_type: str, config: Optional[dict] = None) -> MetricBackend:
    """根据类型和可选配置创建后端实例。"""
    config = config or {}
    backend_type = backend_type.lower().strip()
    if backend_type == "influxdb":
        return InfluxDBMetricBackend(
            url=config.get("influxdb_url"),
            token=config.get("influxdb_token"),
            org=config.get("influxdb_org"),
            bucket=config.get("influxdb_bucket"),
        )
    if backend_type == "sqlite":
        return SQLiteMetricBackend(
            database_url=config.get("metric_sqlite_database_url")
        )
    raise ValueError(f"不支持的指标后端类型: {backend_type}")


def get_metric_backend() -> MetricBackend:
    """获取全局指标存储后端实例。

    首次调用时根据运行时持久化配置或环境变量默认值创建对应后端；
    测试中可通过 `set_metric_backend()` 注入 Mock，或通过
    `switch_metric_backend()` 切换真实后端。
    """
    global _backend
    if _backend is None:
        backend_type = get_metric_backend_type()
        _backend = _create_backend(backend_type)
    return _backend


def set_metric_backend(backend: Optional[MetricBackend]) -> None:
    """设置/替换全局后端实例（主要用于测试）。"""
    global _backend
    _backend = backend


def reset_metric_backend() -> None:
    """重置全局后端为 None，下次 `get_metric_backend()` 会重新初始化。"""
    global _backend
    _backend = None


def close_metric_backend() -> None:
    """关闭当前全局后端并释放连接，避免重复初始化。"""
    global _backend
    if _backend is not None:
        try:
            _backend.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"关闭指标后端失败: {exc}")
        finally:
            _backend = None


def switch_metric_backend(
    backend_type: str,
    config: Optional[dict] = None,
    test_connection: bool = True,
) -> MetricBackend:
    """切换运行时指标后端。

    Args:
        backend_type: "influxdb" 或 "sqlite"。
        config: 可选的覆盖配置，用于测试连接或切换时指定新参数。
        test_connection: 切换前是否先测试目标后端连接，默认 True。

    Returns:
        新创建的后端实例。

    Raises:
        ValueError: 后端类型非法。
        RuntimeError: 连接测试失败（当 test_connection=True 时）。
    """
    global _backend
    backend_type = backend_type.lower().strip()
    if backend_type not in {"influxdb", "sqlite"}:
        raise ValueError(f"非法指标后端类型: {backend_type}，仅支持 influxdb/sqlite")

    new_backend = _create_backend(backend_type, config)

    if test_connection and not new_backend.check_health():
        try:
            new_backend.close()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"目标后端 {backend_type} 连接测试失败")

    old_backend = _backend
    _backend = new_backend

    if old_backend is not None and old_backend is not new_backend:
        try:
            old_backend.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"关闭旧指标后端失败: {exc}")

    # 持久化新后端类型，重启后保持
    set_metric_backend_type(backend_type)
    logger.info(f"指标后端已切换为 {backend_type}")
    return _backend


def _normalize_sample(sample: MetricSample) -> MetricSample:
    """规范化样本：确保时间戳带时区，并将 node_id 从 labels 分离。"""
    ts = sample.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    labels = dict(sample.labels) if sample.labels else {}
    # node_id 由上层传入，不应重复存在于 labels
    labels.pop("node_id", None)

    return MetricSample(
        metric_name=sample.metric_name,
        value=sample.value,
        timestamp=ts,
        labels=labels,
    )


def ingest_metrics(
    node_id: str,
    samples: list[MetricSample],
    backend: Optional[MetricBackend] = None,
) -> tuple[int, int]:
    """将样本列表写入时序数据库（同步路径，并同步驱动告警检测）。

    流式分片：边规范化边按 METRIC_WRITE_BATCH_MAX 分片写入，不一次性物化全部
    normalized 样本到内存；节点在线状态仅由首个写入成功的分片更新一次。

    Args:
        node_id: 节点 ID，写入每个样本的 labels["node_id"]。
        samples: MetricSample 列表。
        backend: 可选后端实例，用于测试注入。

    Returns:
        (accepted, dropped) 计数元组。
    """
    dropped = [0]
    accepted = 0
    node_touched = False
    chunk: list[MetricSample] = []
    for norm in _iter_normalized(node_id, samples, dropped):
        chunk.append(norm)
        if len(chunk) >= METRIC_WRITE_BATCH_MAX:
            acc, extra = _persist_and_publish(
                node_id, chunk, backend=backend, publish=True,
                update_node=not node_touched,
            )
            accepted += acc
            dropped[0] += extra
            if acc > 0:
                node_touched = True
            chunk = []
    if chunk:
        acc, extra = _persist_and_publish(
            node_id, chunk, backend=backend, publish=True,
            update_node=not node_touched,
        )
        accepted += acc
        dropped[0] += extra
    return accepted, dropped[0]


def _iter_normalized(node_id: str, samples, dropped_counter: list):
    """逐条规范化样本并注入 node_id 标签（流式生成器）。

    相比一次性返回完整列表，生成器让调用方边读边分片写入，内存峰值仅为单个
    分片（METRIC_WRITE_BATCH_MAX 条），避免超大批次被整体物化到内存。
    规范化失败的样本计入 ``dropped_counter``（单元素列表，充当可变计数器）。
    """
    for sample in samples:
        try:
            norm = _normalize_sample(sample)
            norm.labels["node_id"] = node_id
            yield norm
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"规范化样本失败 ({sample.metric_name}): {exc}")
            dropped_counter[0] += 1


def _normalize_batch(
    node_id: str, samples: list[MetricSample]
) -> tuple[list[MetricSample], int]:
    """规范化样本并注入 node_id 标签，返回 (规范化列表, 丢弃数)。

    基于流式生成器 ``_iter_normalized`` 实现；适合小批量场景，大批次请直接用
    ``_iter_normalized`` 流式分片，避免整体物化。
    """
    if not samples:
        return [], 0
    dropped = [0]
    normalized = list(_iter_normalized(node_id, samples, dropped))
    return normalized, dropped[0]


def _persist_and_publish(
    node_id: str,
    normalized: list[MetricSample],
    backend: Optional[MetricBackend] = None,
    *,
    publish: bool = True,
    update_node: bool = True,
) -> tuple[int, int]:
    """写入后端、更新节点在线状态、并（可选）发布告警检测事件。

    这是指标摄入的“落库核心”，同步路径与异步 worker 共用，避免逻辑分叉。

    Args:
        publish: True 时逐条发布 MetricIngestedEvent 驱动进程内告警检测；
            异步模式下由 metric_worker 置为 False，改为入队 alert.detect 交给
            detector_worker 评估，避免同一指标被重复检测。
        update_node: True 且 accepted>0 时更新节点在线状态；流式分片摄入时
            仅首个写入成功的分片需要更新，避免重复写库。
    """
    dropped = 0
    writer = backend or get_metric_backend()
    result = writer.write(normalized)
    accepted = result.get("accepted", 0)
    dropped += result.get("dropped", 0)

    if accepted == 0:
        logger.warning("指标摄入失败，整批丢弃（Agent 将缓存并重试）")

    # 指标摄入成功后更新节点在线状态
    if update_node and accepted > 0:
        from app.core.database import engine
        from sqlmodel import select
        from app.models.node import Node
        try:
            with Session(engine) as session:
                node = session.exec(
                    select(Node).where(Node.node_id == node_id, Node.is_deleted == False)  # noqa: E712
                ).first()
                if node is not None:
                    from app.services.node_service import touch_node_online
                    touch_node_online(session, node)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"更新节点在线状态失败: node_id={node_id}, error={exc}")

    if publish:
        # 发布 MetricIngestedEvent，驱动告警检测引擎（异步，不阻塞返回）
        for sample in normalized:
            publish_metric_ingested(
                MetricIngestedEvent(
                    node_id=node_id,
                    metric_name=sample.metric_name,
                    value=float(sample.value),
                    timestamp=sample.timestamp,
                    labels=dict(sample.labels),
                )
            )

    return accepted, dropped


def ingest_metrics_enqueue(
    node_id: str,
    samples: list[MetricSample],
) -> tuple[int, int]:
    """异步摄入：规范化后入队 metric.write 即返回（Phase 3 Step 05）。

    规范化逐条流式进行（可立即检出非法样本计入 dropped）；落库与检测由后台
    worker 完成。按 METRIC_WRITE_BATCH_MAX 流式分片入队，不一次性物化全部样本。

    Returns:
        (normalized_count, dropped)：乐观 accepted（规范化成功数），真实落库结果由
        worker 统计/DLQ 观测。
    """
    from app.core.task_queue import get_task_queue

    dropped = [0]
    queue = get_task_queue()
    total = 0
    chunk: list[MetricSample] = []
    for norm in _iter_normalized(node_id, samples, dropped):
        chunk.append(norm)
        if len(chunk) >= METRIC_WRITE_BATCH_MAX:
            _enqueue_metric_chunk(queue, node_id, chunk)
            total += len(chunk)
            chunk = []
    if chunk:
        _enqueue_metric_chunk(queue, node_id, chunk)
        total += len(chunk)
    return total, dropped[0]


def _enqueue_metric_chunk(queue, node_id: str, chunk: list[MetricSample]) -> None:
    """将一个样本分片序列化并入队 metric.write。"""
    payload = {
        "node_id": node_id,
        "samples": [
            s.to_dict() if hasattr(s, "to_dict") else _sample_to_dict(s) for s in chunk
        ],
    }
    queue.enqueue(QUEUE_METRIC_WRITE, payload)


def _sample_to_dict(sample: MetricSample) -> dict:
    """后端 MetricSample（app.models.metric）序列化为可 JSON 的字典。"""
    ts = sample.timestamp
    ts_str = ts.isoformat()
    if ts_str.endswith("+00:00"):
        ts_str = ts_str[:-6] + "Z"
    return {
        "metric_name": sample.metric_name,
        "value": sample.value,
        "timestamp": ts_str,
        "labels": dict(sample.labels) if sample.labels else {},
    }


def query_metrics(
    node_id: str,
    metric_name: str,
    start: datetime,
    end: datetime,
    labels: Optional[dict] = None,
    backend: Optional[MetricBackend] = None,
) -> list[MetricSample]:
    """查询指标样本（供指标查询 API 使用）。

    入口对 node_id / metric_name / label value 做白名单校验，非法值抛 ValueError；
    开放 API 与 Web 指标接口均经此函数，从源头阻止 Flux 注入。
    """
    labels = labels or {}
    # 安全防线：所有会拼进 Flux 的字符串值必须过白名单，拒绝注入 payload。
    _validate_query_value("node_id", node_id)
    _validate_query_value("metric_name", metric_name)
    for _key, _val in labels.items():
        _validate_query_value("labels", _val)

    writer = backend or get_metric_backend()
    return writer.query(
        node_id=node_id,
        metric_name=metric_name,
        start=start,
        end=end,
        labels=labels,
    )
