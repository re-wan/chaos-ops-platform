"""指标接收与查询 API 路由。"""

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import get_current_node, get_current_user
from app.core.logger import get_logger
from app.models.metric import IngestResponse, MetricBatch
from app.models.node import Node
from app.models.user import User
from app.services.metric_query import (
    execute_query,
    get_latest_value,
    get_metric_names,
)
from app.services.metrics_ingest import ingest_metrics, ingest_metrics_enqueue

try:
    from app.services.optimization_lifecycle import get_check_interval_overrides
except ImportError:
    # 三版物理分包删除优化模块后，采集间隔覆盖恒为空。
    def get_check_interval_overrides(node_id: str) -> dict[str, int]:
        return {}


router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])
logger = get_logger("api.metrics")

# SSE 实时推送间隔（秒）
_LIVE_PUSH_INTERVAL_SECONDS = 10

# 实时推送默认关心的指标
_LIVE_METRIC_NAMES = [
    "cpu_percent",
    "memory_percent",
    "disk_usage_percent",
]


def _verify_node_ownership(current_node: Node, node_id: str) -> None:
    """校验当前 Agent Token 所属节点与请求中的 node_id 是否匹配。"""
    if current_node.node_id != node_id:
        logger.warning(
            f"Agent 越权上报指标: token 属于 {current_node.node_id}, "
            f"请求 node_id={node_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="node_id 与认证信息不匹配",
        )


def _dispatch_ingest(node_id: str, samples) -> tuple[int, int]:
    """根据任务队列状态选择同步/异步摄入路径。

    - 任务队列异步可用（Redis）→ 规范化后入队即返回，落库/检测由后台 worker 完成，
      请求线程不再阻塞在时序库 IO 上。
    - 否则（默认，含测试环境）→ 走原有同步路径，语义与 Phase 1 完全一致。
    异步分支若运行期异常，自动回退同步路径，保证不丢数据；若队列水位已满
    （背压），则返回 503 让 Agent 退避重试，避免 Redis 内存无限增长。
    """
    try:
        from app.core.task_queue import QueueFullError, get_task_queue

        if get_task_queue().is_async():
            return ingest_metrics_enqueue(node_id, samples)
    except QueueFullError as exc:
        # 背压：拒绝入队并返回 503，Agent 端已有退避/缓存重试承接，避免雪崩。
        logger.warning(f"任务队列水位已满，拒绝入队(503): {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="任务队列繁忙，请稍后重试",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"异步摄入失败，回退同步路径: {exc}")
    return ingest_metrics(node_id=node_id, samples=samples)


@router.post("/ingest", response_model=IngestResponse, response_model_exclude_none=True)
def ingest_batch(
    body: MetricBatch,
    current_node: Node = Depends(get_current_node),
) -> dict:
    """Agent 批量上报指标。

    需要 Agent Token 认证；Server 会校验 Token 对应的 node_id 与请求体是否一致，
    并将合法指标写入 InfluxDB。

    响应附带 collector_intervals（仅当该节点存在 check_interval_override 覆盖键时），
    Agent 据此在运行中调整对应采集器间隔（批 14，pull 模型配置下发）。
    """
    _verify_node_ownership(current_node, body.node_id)

    accepted, dropped = _dispatch_ingest(body.node_id, body.samples)
    response = {"accepted": accepted, "dropped": dropped}

    # 配置下发：该节点的采集间隔覆盖（无覆盖键时不返回该字段）
    overrides = get_check_interval_overrides(body.node_id)
    if overrides:
        response["collector_intervals"] = overrides

    return response


@router.post("/push", response_model=IngestResponse, response_model_exclude_none=True)
def push_metrics(
    body: MetricBatch,
    current_node: Node = Depends(get_current_node),
) -> dict:
    """用户应用推送自定义指标。

    MVP 阶段复用 Agent Token 认证与 MetricBatch 格式，
    由 Agent 或被监控应用持有对应节点的 Agent Token 后推送。
    """
    _verify_node_ownership(current_node, body.node_id)

    accepted, dropped = _dispatch_ingest(body.node_id, body.samples)
    return {"accepted": accepted, "dropped": dropped}


@router.get("/query")
def query_metrics_endpoint(
    node_id: str,
    metric: str,
    labels: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    step: Optional[str] = None,
    aggregator: str = Query(default="avg", pattern=r"^(avg|max|min|sum|count|rate)$"),
    current_user: User = Depends(get_current_user),
) -> dict:
    """查询历史指标（Session Token 认证）。"""
    try:
        return execute_query(
            node_id=node_id,
            metric=metric,
            labels_str=labels,
            start_str=start,
            end_str=end,
            step_str=step,
            aggregator=aggregator,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.get("/latest")
def latest_metric_endpoint(
    node_id: str,
    metric: str,
    labels: Optional[str] = None,
    current_user: User = Depends(get_current_user),
) -> dict:
    """查询指标最新值（Session Token 认证）。"""
    try:
        result = get_latest_value(
            node_id=node_id,
            metric=metric,
            labels_str=labels,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到指标数据",
        )
    return result


@router.get("/names")
def list_metric_names_endpoint(
    node_id: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    """列出节点可用指标名（Session Token 认证）。"""
    try:
        names = get_metric_names(node_id=node_id)
    except ValueError as e:
        # node_id 含非法字符（注入探测）时与查询端点一致返回 400，避免 500。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    return {"node_id": node_id, "metrics": names}


async def _live_metrics_generator(node_id: str) -> AsyncGenerator[str, None]:
    """SSE 实时推送生成器。"""
    from app.services.metrics_ingest import query_metrics

    try:
        while True:
            now = datetime.now(timezone.utc)
            start = now - timedelta(minutes=5)

            metrics = {}
            for metric_name in _LIVE_METRIC_NAMES:
                try:
                    samples = query_metrics(
                        node_id=node_id,
                        metric_name=metric_name,
                        start=start,
                        end=now,
                    )
                    if samples:
                        latest = max(samples, key=lambda s: s.timestamp)
                        metrics[metric_name] = float(latest.value)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"实时推送查询失败 ({metric_name}): {exc}")

            payload = {
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "metrics": metrics,
            }
            yield f"data: {JSONResponse(content=payload).body.decode()}\n\n"

            await asyncio.sleep(_LIVE_PUSH_INTERVAL_SECONDS)
    except (asyncio.CancelledError, GeneratorExit):
        logger.info(f"实时推送连接断开: {node_id}")
        raise


@router.get("/live")
def live_metrics_endpoint(
    node_id: str,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """SSE 实时推送节点最新指标（Session Token 认证）。"""
    return StreamingResponse(
        _live_metrics_generator(node_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
