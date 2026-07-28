#!/usr/bin/env python3
"""性能基线 / 缩短版压测工具（Phase 3 Step 05）。

度量驱动：先跑本工具取得当前基线数字，再做针对性优化，最后用同一工具回归验证。
本工具**进程内**运行，不依赖真实 InfluxDB / Redis / 外部服务，便于在任何环境
产出可复现的基线（CI、开发机）。**全量** 50,000 样本/秒 × 8 小时老化请使用
``perf_load.py`` 对真实部署执行（hardening 专项）。

用法示例：

    python backend/scripts/perf_baseline.py --nodes 200 --duration 10 --batch 100 --rules 500
    python backend/scripts/perf_baseline.py --backend sqlite --json baseline.json

产出指标：

- 同步摄入（ingest_metrics）：吞吐（样本/秒）、P50/P99 单次延迟（请求线程含落库）。
- 异步入队（ingest_metrics_enqueue + fakeredis worker）：请求侧入队吞吐、worker 排空速率。
- 告警检测：在 R 条规则规模下，单事件评估（含规则索引命中）的 P50/P99 延迟。
- 进程 RSS（内存）前后快照。
"""

import argparse
import gc
import json
import os
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# 将 backend 加入路径，以便导入 app 模块
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 在导入 app 前固定测试化配置，避免访问真实服务与文件型 sqlite。
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("METRIC_BACKEND_TYPE", "sqlite")
os.environ.setdefault("METRIC_BACKEND_HEALTH_CHECK_AT_STARTUP", "false")
os.environ.setdefault("ALERT_DETECTOR_ENABLED", "false")
os.environ.setdefault("LOCAL_AGENT_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "perf-baseline-secret-key-32bytes-minimum")
os.environ.setdefault("TASK_QUEUE_ENABLED", "off")  # 同步基线默认关闭队列

import psutil  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app.core import database as database_module  # noqa: E402
from app.core.metric_cache import get_metric_cache  # noqa: E402
from app.models.metric import MetricSample  # noqa: E402
from app.services import metrics_ingest  # noqa: E402


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def rss_mb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def setup_engine():
    """构造文件型 SQLite 引擎并设为全局。

    使用临时文件 + SQLAlchemy 默认 QueuePool：每个线程获取独立连接，避免
    ``:memory:`` + StaticPool 在多线程下共享同一连接导致的 InterfaceError。
    """
    import tempfile

    db_path = Path(tempfile.gettempdir()) / f"chaosops_perf_{os.getpid()}.db"
    if db_path.exists():
        db_path.unlink()
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    database_module.engine = engine
    return engine, db_path


def create_nodes(engine, count: int) -> list[str]:
    """预先创建节点行，返回 node_id 列表，使摄入的节点在线 touch 走快速索引路径。"""
    from app.core.agent_auth import create_node_with_tokens

    node_ids: list[str] = []
    with Session(engine) as session:
        for i in range(count):
            node = create_node_with_tokens(session, name=f"perf-node-{i}")
            node_ids.append(node.node_id)
    return node_ids


def make_samples(node_id: str, batch: int, metrics: int = 5) -> list[MetricSample]:
    now = datetime.now(timezone.utc)
    samples = []
    for i in range(batch):
        samples.append(
            MetricSample(
                metric_name=f"metric_{i % metrics}",
                value=float((i % 100) + 1),
                timestamp=now,
                labels={"node_id": node_id},
            )
        )
    return samples


def bench_ingest_sync(args, node_ids: list[str]) -> dict:
    """同步摄入基线：N 个并发‘节点’在 duration 内持续上报，统计吞吐与延迟。"""
    from app.core.metric_backend import MockMetricBackend

    if args.backend == "mock":
        metrics_ingest.set_metric_backend(MockMetricBackend())
    else:
        from app.core.metric_sqlite import SQLiteMetricBackend

        metrics_ingest.set_metric_backend(SQLiteMetricBackend())

    duration = args.duration
    batch = args.batch
    stop_at = time.time() + duration

    accepted_total = 0
    calls = 0
    latencies_ms: list[float] = []
    lock = threading.Lock()

    def worker(node_id: str) -> None:
        nonlocal accepted_total, calls
        local_acc = 0
        local_calls = 0
        local_lat: list[float] = []
        while time.time() < stop_at:
            samples = make_samples(node_id, batch)
            t0 = time.perf_counter()
            acc, _ = metrics_ingest.ingest_metrics(node_id, samples)
            dt = (time.perf_counter() - t0) * 1000.0
            local_acc += acc
            local_calls += 1
            local_lat.append(dt)
        with lock:
            accepted_total += local_acc
            calls += local_calls
            latencies_ms.extend(local_lat)

    threads = [threading.Thread(target=worker, args=(nid,)) for nid in node_ids]
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t_start

    return {
        "mode": f"sync({args.backend})",
        "nodes": len(node_ids),
        "batch": batch,
        "duration_s": round(elapsed, 2),
        "accepted_samples": accepted_total,
        "throughput_samples_per_s": round(accepted_total / elapsed, 0) if elapsed else 0,
        "calls": calls,
        "p50_ms": round(percentile(latencies_ms, 0.50), 3),
        "p99_ms": round(percentile(latencies_ms, 0.99), 3),
    }


def bench_ingest_async(args, node_ids: list[str]) -> dict:
    """异步入队基线：请求侧 ingest_metrics_enqueue 吞吐 + fakeredis worker 排空速率。"""
    import fakeredis

    from app.core import task_queue as tq_module
    from app.core.metric_backend import MockMetricBackend
    from app.core.queues import QUEUE_METRIC_WRITE
    from app.core.task_queue import TaskQueue

    metrics_ingest.set_metric_backend(MockMetricBackend())

    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    q = TaskQueue(
        redis_client=client, block_timeout=1, concurrency=max(2, args.workers), backoff_base=0.0
    )
    tq_module.set_task_queue(q)

    drained_jobs = {"n": 0}

    def counting_handler(payload):
        drained_jobs["n"] += 1

    q.register_handler(QUEUE_METRIC_WRITE, counting_handler)
    q.start()

    duration = args.duration
    batch = args.batch
    stop_at = time.time() + duration

    enqueued_samples = 0
    enqueued_jobs = 0
    lock = threading.Lock()

    def worker(node_id: str) -> None:
        nonlocal enqueued_samples, enqueued_jobs
        local_s = 0
        local_j = 0
        while time.time() < stop_at:
            samples = make_samples(node_id, batch)
            acc, _ = metrics_ingest.ingest_metrics_enqueue(node_id, samples)
            local_s += acc
            local_j += 1
        with lock:
            enqueued_samples += local_s
            enqueued_jobs += local_j

    threads = [threading.Thread(target=worker, args=(nid,)) for nid in node_ids]
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    enqueue_elapsed = time.perf_counter() - t_start

    # 等待 worker 排空（最多 30s）
    deadline = time.time() + 30.0
    while time.time() < deadline and drained_jobs["n"] < enqueued_jobs:
        time.sleep(0.02)
    q.stop()
    tq_module.reset_task_queue()

    return {
        "mode": "async_enqueue(fakeredis)",
        "nodes": len(node_ids),
        "batch": batch,
        "enqueue_throughput_samples_per_s": (
            round(enqueued_samples / enqueue_elapsed, 0) if enqueue_elapsed else 0
        ),
        "enqueued_jobs": enqueued_jobs,
        "drained_jobs": drained_jobs["n"],
        "drain_complete": drained_jobs["n"] >= enqueued_jobs,
    }


def bench_detection(args) -> dict:
    """告警检测基线：R 条规则规模下，单事件评估（规则索引命中）延迟。"""
    from app.core.events import MetricIngestedEvent
    from app.models.alert_rule import AlertRule
    from app.services.alert_detector import AlertDetector

    engine = database_module.engine
    # 写入 R 条启用规则，分布在 50 个指标上。
    with Session(engine) as session:
        for i in range(args.rules):
            metric = f"metric_{i % 50}"
            session.add(
                AlertRule(
                    name=f"perf-rule-{i}",
                    scope="global",
                    condition_type="json_dsl",
                    condition=json.dumps(
                        {"metric": metric, "op": ">", "value": 10**9}  # 永不满足，避免 firing 副作用
                    ),
                    severity="warning",
                    enabled=True,
                )
            )
        session.commit()

    detector = AlertDetector(engine)
    detector.reload_rules()
    rules_indexed = detector.rules_indexed

    # 预热缓存
    cache = get_metric_cache()
    for m in range(50):
        cache.update("node-perf", f"metric_{m}", {}, 1.0, datetime.now(timezone.utc))

    latencies_ms: list[float] = []
    events = max(1000, args.rules * 4)
    now = datetime.now(timezone.utc)
    for k in range(events):
        ev = MetricIngestedEvent(
            node_id="node-perf",
            metric_name=f"metric_{k % 50}",
            value=1.0,
            timestamp=now,
            labels={},
        )
        t0 = time.perf_counter()
        detector._handle_event(ev)
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)
    detector.stop()

    return {
        "mode": "detection",
        "rules": args.rules,
        "rules_indexed": rules_indexed,
        "events": events,
        "p50_ms": round(percentile(latencies_ms, 0.50), 4),
        "p99_ms": round(percentile(latencies_ms, 0.99), 4),
        "avg_ms": round(statistics.mean(latencies_ms), 4) if latencies_ms else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="ChaosOps 性能基线/缩短版压测")
    parser.add_argument("--nodes", type=int, default=50, help="模拟并发节点数")
    parser.add_argument("--duration", type=int, default=5, help="每个摄入场景持续秒数")
    parser.add_argument("--batch", type=int, default=100, help="每批样本数")
    parser.add_argument("--rules", type=int, default=500, help="告警规则数（检测基线）")
    parser.add_argument(
        "--backend", choices=["mock", "sqlite"], default="mock", help="指标后端"
    )
    parser.add_argument("--workers", type=int, default=2, help="异步 worker 线程数")
    parser.add_argument("--json", type=str, default=None, help="可选：将结果写入 JSON 文件")
    parser.add_argument(
        "--skip-async", action="store_true", help="跳过异步场景（无 fakeredis 时）"
    )
    args = parser.parse_args()

    engine, db_path = setup_engine()
    node_ids = create_nodes(engine, args.nodes)

    gc.collect()
    rss_before = rss_mb()
    results: dict = {"args": vars(args), "rss_before_mb": round(rss_before, 1)}

    print("=" * 70)
    print(f"ChaosOps 性能基线  nodes={args.nodes} batch={args.batch} "
          f"rules={args.rules} backend={args.backend}")
    print("=" * 70)

    sync = bench_ingest_sync(args, node_ids)
    results["ingest_sync"] = sync
    print(f"[同步摄入 {sync['mode']}] "
          f"吞吐={sync['throughput_samples_per_s']:.0f} samples/s  "
          f"P50={sync['p50_ms']}ms  P99={sync['p99_ms']}ms  "
          f"accepted={sync['accepted_samples']}")

    if not args.skip_async:
        try:
            async_r = bench_ingest_async(args, node_ids)
            results["ingest_async"] = async_r
            print(f"[异步入队] 请求侧吞吐="
                  f"{async_r['enqueue_throughput_samples_per_s']:.0f} samples/s  "
                  f"排空={async_r['drained_jobs']}/{async_r['enqueued_jobs']} "
                  f"complete={async_r['drain_complete']}")
        except Exception as exc:  # noqa: BLE001
            print(f"[异步入队] 跳过（{exc}）")
            results["ingest_async"] = {"error": str(exc)}

    det = bench_detection(args)
    results["detection"] = det
    print(f"[检测] rules={det['rules']} indexed={det['rules_indexed']}  "
          f"单事件 P50={det['p50_ms']}ms  P99={det['p99_ms']}ms  avg={det['avg_ms']}ms")

    rss_after = rss_mb()
    results["rss_after_mb"] = round(rss_after, 1)
    results["rss_delta_mb"] = round(rss_after - rss_before, 1)
    print(f"[内存] RSS before={rss_before:.1f}MB after={rss_after:.1f}MB "
          f"delta={rss_after - rss_before:+.1f}MB")
    print("=" * 70)

    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(f"结果已写入 {args.json}")

    # 清理临时数据库文件
    try:
        database_module.engine.dispose()
        if db_path.exists():
            db_path.unlink()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
