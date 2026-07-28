#!/usr/bin/env python3
"""全量压测工具（Phase 3 Step 05，hardening 专项）。

> 本工具针对**真实部署**的 ChaosOps Server 发起 HTTP 压测，用于 Phase 3 末尾
> hardening 的统一验收。请勿把其指标塞进 pytest 常规套件（会卡死/超时）。

设计目标场景（docs/core/PERFORMANCE_DESIGN.md §3.2）：

- 200+ 节点、50,000+ 样本/秒持续写入；
- 告警风暴 1000 条 firing/分钟；
- 8 小时加速老化（用 --time-compression 折算，例如 16x → 30 分钟模拟 8 小时）。

用法：

    # 单节点冒烟（最快验证通路）
    python backend/scripts/perf_load.py \\
        --server-url http://127.0.0.1:8000 --node-id node-1 --token <AGENT_TOKEN> \\
        --rate 2000 --duration 30

    # 200 节点全量（先注册 200 个节点，导出 tokens.jsonl）
    python backend/scripts/perf_load.py \\
        --server-url https://chaosops.example.com --tokens-file tokens.jsonl \\
        --nodes 200 --rate 50000 --duration 28800 --batch 500

tokens.jsonl 格式（每行一个 JSON）：{"node_id": "...", "agent_token": "..."}

产出：周期打印 + 最终汇总（实际吞吐、P50/P99 请求延迟、错误率、Server 返回的
accepted/dropped 累计）。请同时观察 Server 端 CPU/内存/慢查询日志与队列 DLQ。
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from datetime import datetime, timezone

import httpx


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def load_nodes(args) -> list[dict]:
    """加载节点凭据。优先 --tokens-file，否则单节点 --token/--node-id。"""
    if args.tokens_file:
        nodes: list[dict] = []
        with open(args.tokens_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                nodes.append(json.loads(line))
        if args.nodes > 0:
            nodes = nodes[: args.nodes]
        if not nodes:
            raise SystemExit("tokens-file 为空")
        return nodes
    if not args.token or not args.node_id:
        raise SystemExit("请提供 --tokens-file，或 --token 与 --node-id")
    return [{"node_id": args.node_id, "agent_token": args.token}]


def make_payload(node_id: str, batch: int, metrics: int, spike: bool) -> dict:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    samples = []
    for i in range(batch):
        # 5% 概率制造 spike，便于触发告警风暴（可选）。
        val = random.random() * 100
        if spike and random.random() < 0.05:
            val = 1000.0
        samples.append(
            {
                "metric_name": f"metric_{i % metrics}",
                "value": val,
                "timestamp": now,
                "labels": {},
            }
        )
    return {"node_id": node_id, "samples": samples}


async def worker(
    client: httpx.AsyncClient,
    url: str,
    node: dict,
    args,
    stop_at: float,
    stats: dict,
) -> None:
    headers = {"Authorization": f"Bearer {node['agent_token']}"}
    node_id = node["node_id"]
    while time.time() < stop_at:
        payload = make_payload(node_id, args.batch, args.metrics, args.spike)
        t0 = time.perf_counter()
        try:
            resp = await client.post(url, json=payload, headers=headers)
            dt = (time.perf_counter() - t0) * 1000.0
            stats["latencies_ms"].append(dt)
            if resp.status_code == 200:
                body = resp.json()
                stats["accepted"] += int(body.get("accepted", 0))
                stats["dropped"] += int(body.get("dropped", 0))
                stats["requests"] += 1
            else:
                stats["errors"] += 1
                stats["error_status"][resp.status_code] = (
                    stats["error_status"].get(resp.status_code, 0) + 1
                )
        except Exception:  # noqa: BLE001
            stats["errors"] += 1
            stats["exceptions"] += 1
        # 简单限速：按目标速率均摊到每个 worker
        await asyncio.sleep(args._per_worker_interval)


async def reporter(stop_at: float, stats: dict, started: float) -> None:
    last_req = 0
    while time.time() < stop_at:
        await asyncio.sleep(5.0)
        now = time.time()
        req = stats["requests"]
        rps = (req - last_req) / 5.0
        last_req = req
        elapsed = now - started
        print(
            f"[{elapsed:7.0f}s] req={req} rps={rps:7.1f} "
            f"accepted={stats['accepted']} dropped={stats['dropped']} "
            f"errors={stats['errors']}",
            flush=True,
        )


async def amain() -> None:
    parser = argparse.ArgumentParser(description="ChaosOps 全量压测（hardening）")
    parser.add_argument("--server-url", default="http://127.0.0.1:8000")
    parser.add_argument("--tokens-file", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--node-id", default=None)
    parser.add_argument("--nodes", type=int, default=0)
    parser.add_argument("--rate", type=int, default=2000, help="目标总样本/秒")
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--metrics", type=int, default=10)
    parser.add_argument("--duration", type=int, default=60, help="持续秒数")
    parser.add_argument("--spike", action="store_true", help="注入尖峰以触发告警风暴")
    parser.add_argument(
        "--time-compression",
        type=int,
        default=1,
        help="老化折算倍率：Nx 表示用 1/N 时间模拟长周期（仅打印提示）",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    nodes = load_nodes(args)
    n_workers = len(nodes)
    # 每个 worker 的出站间隔：rate 样本/秒 ÷ batch = 总请求/秒；均摊到 worker。
    total_rps = max(1.0, args.rate / max(1, args.batch))
    per_worker_rps = total_rps / n_workers
    args._per_worker_interval = 1.0 / per_worker_rps if per_worker_rps > 0 else 0.0

    url = f"{args.server_url.rstrip('/')}/api/v1/metrics/ingest"
    print("=" * 72)
    print(f"ChaosOps 全量压测  server={args.server_url}")
    print(f"  nodes={n_workers} rate={args.rate} samples/s batch={args.batch} "
          f"metrics={args.metrics} duration={args.duration}s "
          f"compression={args.time_compression}x")
    if args.time_compression > 1:
        emulated = args.duration * args.time_compression
        print(f"  -> 本次 {args.duration}s 按 {args.time_compression}x 折算，"
              f"模拟约 {emulated}s（{emulated/3600:.1f}h）老化负载")
    print("=" * 72)

    stats: dict = {
        "requests": 0,
        "accepted": 0,
        "dropped": 0,
        "errors": 0,
        "exceptions": 0,
        "error_status": {},
        "latencies_ms": [],
    }

    limits = httpx.Limits(max_connections=n_workers * 2, max_keepalive_connections=n_workers)
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        started = time.time()
        stop_at = started + args.duration
        tasks = [
            asyncio.create_task(worker(client, url, node, args, stop_at, stats))
            for node in nodes
        ]
        tasks.append(asyncio.create_task(reporter(stop_at, stats, started)))
        await asyncio.gather(*tasks)
        elapsed = time.time() - started

    lat = stats["latencies_ms"]
    actual_rate = stats["accepted"] / elapsed if elapsed else 0
    print("=" * 72)
    print("压测汇总")
    print(f"  持续时间      : {elapsed:.1f}s")
    print(f"  请求数        : {stats['requests']} (errors={stats['errors']}, "
          f"exceptions={stats['exceptions']})")
    if stats["error_status"]:
        print(f"  错误状态码    : {stats['error_status']}")
    print(f"  Server accepted: {stats['accepted']}")
    print(f"  Server dropped : {stats['dropped']}")
    print(f"  实际吞吐      : {actual_rate:.0f} samples/s "
          f"(目标 {args.rate})")
    print(f"  请求延迟 P50  : {percentile(lat, 0.50):.1f}ms  "
          f"P99={percentile(lat, 0.99):.1f}ms  avg={statistics.mean(lat):.1f}ms"
          if lat else "  无成功请求")
    print("=" * 72)
    if stats["dropped"] > 0 or stats["errors"] > 0:
        print("⚠️  出现丢点或错误：请检查 Server 队列 DLQ、慢查询日志与资源水位。")
        sys.exit(2)


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
