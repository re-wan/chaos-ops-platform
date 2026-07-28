"""ChaosOps Agent 入口。

支持两种模式：
1. 注册模式：使用 Install Key 向 Server 注册并保存配置。
2. 守护模式：加载已有配置，启动指标采集与上报循环。
"""

import argparse
import logging
import platform
import socket
import threading
import time
from pathlib import Path
from typing import Optional


from agent.app.collectors import (
    DiskIOCollector,
    HostCollector,
    HTTPCollector,
    LoadCollector,
    NetstatCollector,
    NetworkCollector,
    ProcessCollector,
    ServiceCollector,
)
from agent.app.collectors.base import BaseCollector, MetricSample
from agent.app.config import get_collector_config, load_config, save_config
from agent.app.executor import create_executor_from_config
from agent.app.reporter import register_agent
from agent.app.updater import UpdateManager
from agent.app.sender import MetricSender
from agent.app.aggregator import MetricAggregator

logger = logging.getLogger("agent.main")

# Server 下发采集间隔的合理范围钳制（秒）：防止异常覆盖键打爆采集循环
# 或把间隔拉到不合理长度。
MIN_COLLECTOR_INTERVAL = 5
MAX_COLLECTOR_INTERVAL = 3600


def build_collectors(config: dict) -> list[BaseCollector]:
    """根据配置构造采集器列表。"""
    collector_cfg = get_collector_config(config)
    collectors: list[BaseCollector] = []

    host_cfg = collector_cfg["collectors"].get("host", {})
    if host_cfg.get("enabled", True):
        collectors.append(
            HostCollector(interval=host_cfg.get("interval"))
        )

    network_cfg = collector_cfg["collectors"].get("network", {})
    if network_cfg.get("enabled", True):
        collectors.append(
            NetworkCollector(interval=network_cfg.get("interval"))
        )

    http_cfg = collector_cfg["collectors"].get("http", {})
    if http_cfg.get("enabled", True):
        collectors.append(
            HTTPCollector(
                targets=http_cfg.get("targets", []),
                interval=http_cfg.get("interval"),
                timeout=float(http_cfg.get("timeout", 10.0)),
            )
        )

    process_cfg = collector_cfg["collectors"].get("process", {})
    if process_cfg.get("enabled", True):
        collectors.append(
            ProcessCollector(
                interval=process_cfg.get("interval"),
                top_n=int(process_cfg.get("top_n", 10)),
            )
        )

    load_cfg = collector_cfg["collectors"].get("load", {})
    if load_cfg.get("enabled", True):
        collectors.append(
            LoadCollector(interval=load_cfg.get("interval"))
        )

    diskio_cfg = collector_cfg["collectors"].get("diskio", {})
    if diskio_cfg.get("enabled", True):
        collectors.append(
            DiskIOCollector(interval=diskio_cfg.get("interval"))
        )

    service_cfg = collector_cfg["collectors"].get("service", {})
    if service_cfg.get("enabled", True):
        collectors.append(
            ServiceCollector(
                interval=service_cfg.get("interval"),
                services=service_cfg.get("services", []),
                timeout_seconds=float(service_cfg.get("timeout_seconds", 3.0)),
            )
        )

    netstat_cfg = collector_cfg["collectors"].get("netstat", {})
    if netstat_cfg.get("enabled", True):
        collectors.append(
            NetstatCollector(interval=netstat_cfg.get("interval"))
        )

    return collectors


def register_and_save_config(
    server_url: str,
    install_key: str,
    config_dir: Path,
) -> dict:
    """注册 Agent 并保存配置到本地。"""
    print(f"正在向 Server 注册: {server_url}")
    version = "0.1.0"
    result = register_agent(
        server_url=server_url,
        install_key=install_key,
        hostname=socket.gethostname(),
        os=platform.system().lower(),
        arch=platform.machine(),
        version=version,
    )

    config = {
        "server_url": server_url,
        "agent_token": result["agent_token"],
        "node_id": result["node_id"],
        "heartbeat_interval": result["heartbeat_interval"],
        "version": version,
    }

    config_path = config_dir / "config.json"
    save_config(config, config_path)
    print(f"注册成功，节点 ID: {result['node_id']}")
    print(f"配置已保存到: {config_path}")
    return config


class MetricAgent:
    """指标采集与上报守护进程。"""

    def __init__(self, config: dict, config_dir: Path):
        self.config = config
        self.config_dir = config_dir
        self.node_id = config["node_id"]
        self.server_url = config["server_url"]
        self.agent_token = config["agent_token"]

        collector_cfg = get_collector_config(config)
        self.collectors = build_collectors(config)
        self.sender = MetricSender(
            server_url=self.server_url,
            agent_token=self.agent_token,
            node_id=self.node_id,
            batch_size=collector_cfg["batch"]["max_samples"],
            flush_interval_seconds=collector_cfg["batch"]["flush_interval_seconds"],
            timeout_seconds=float(collector_cfg["sender"]["timeout_seconds"]),
            cache_dir=config_dir / "metric_cache",
            max_cache_size_mb=collector_cfg["buffer"]["max_size_mb"],
            max_cache_age_seconds=collector_cfg["buffer"]["max_age_seconds"],
            intervals_handler=self._apply_collector_intervals,
        )
        # Phase 3 Step 05：本地聚合，降低上报量；可通过 aggregation.enabled=false 关闭。
        agg_cfg = collector_cfg.get("aggregation", {}) or {}
        if agg_cfg.get("enabled", True):
            self.aggregator: Optional[MetricAggregator] = MetricAggregator(
                window_seconds=int(agg_cfg.get("window_seconds", 10)),
                max_points_per_key=int(agg_cfg.get("max_points_per_key", 10000)),
                emit_max=bool(agg_cfg.get("emit_max", True)),
            )
        else:
            self.aggregator = None
        # 任务执行器（自愈/远程执行）：以独立 daemon 线程运行轮询循环。
        # 任务 5 Docker 端到端验证发现此前 executor 从未接入主循环（死代码），
        # 导致远程 Agent 上自愈任务永远停在 running。
        self.executor = create_executor_from_config(config)
        self._executor_thread: Optional[threading.Thread] = None
        self.running = False

    def _apply_collector_intervals(self, intervals: dict) -> None:
        """应用 Server 下发的采集间隔覆盖（{metric_name: 间隔秒}）。

        按 metric_name 反查其所属采集器（collector.metric_names），运行中更新
        interval 并持久化到本地 config.json（重启不丢）；未列出的采集器保持
        现状，未知 metric 只告警不崩。间隔值钳制到 [5, 3600] 秒。
        """
        changed: dict[str, int] = {}
        for metric, raw_seconds in intervals.items():
            collector = self._find_collector_for_metric(metric)
            if collector is None:
                logger.warning(f"收到未知指标的间隔下发，已跳过: {metric}")
                continue
            try:
                seconds = int(float(raw_seconds))
            except (TypeError, ValueError):
                logger.warning(f"非法间隔值，已跳过: {metric}={raw_seconds!r}")
                continue
            seconds = max(MIN_COLLECTOR_INTERVAL, min(MAX_COLLECTOR_INTERVAL, seconds))
            if collector.interval != seconds:
                logger.info(
                    f"采集器 {collector.name} 间隔按 Server 下发调整: "
                    f"{collector.interval}s -> {seconds}s（指标 {metric}）"
                )
                collector.interval = seconds
                changed[collector.name] = seconds

        if changed:
            self._persist_collector_intervals(changed)

    def _find_collector_for_metric(self, metric_name: str) -> Optional[BaseCollector]:
        """按指标名找到发射它的采集器，未匹配返回 None。"""
        for collector in self.collectors:
            if metric_name in collector.metric_names:
                return collector
        return None

    def _persist_collector_intervals(self, changed: dict[str, int]) -> None:
        """将变更的采集器间隔写回本地 config.json（save_config 原子写）。

        持久化失败只告警：运行中间隔已生效，最多重启后回退到旧配置。
        """
        collectors_cfg = self.config.setdefault("collectors", {})
        for name, seconds in changed.items():
            collectors_cfg.setdefault(name, {})["interval"] = seconds
        try:
            save_config(self.config, self.config_dir / "config.json")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"持久化采集间隔配置失败: {exc}")

    def _collect_once(self) -> list[MetricSample]:
        """执行一轮采集，单个采集器异常不影响其他采集器。"""
        samples: list[MetricSample] = []
        for collector in self.collectors:
            try:
                collector_samples = collector.collect()
                if collector_samples:
                    samples.extend(collector_samples)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"采集器 {collector.name} 异常: {exc}")
        return samples

    def run(self) -> None:
        """启动采集与上报循环。"""
        logger.info(f"Agent 启动，节点: {self.node_id}, Server: {self.server_url}")
        self.running = True

        # 启动任务执行器线程（拉取-执行-上报自愈/远程执行任务）
        self._executor_thread = threading.Thread(
            target=self.executor.run, name="task-executor", daemon=True
        )
        self._executor_thread.start()

        # 初始启动时先补发缓存
        self.sender.send_cached()

        next_collect_times: dict[str, float] = {}

        try:
            while self.running:
                now = time.monotonic()

                for collector in self.collectors:
                    last = next_collect_times.get(collector.name, 0)
                    if now >= last:
                        try:
                            samples = collector.collect()
                            if samples:
                                if self.aggregator is not None:
                                    self.aggregator.add_many(samples)
                                else:
                                    self.sender.enqueue_many(samples)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f"采集器 {collector.name} 异常: {exc}")
                        next_collect_times[collector.name] = now + collector.interval

                # 聚合窗口到期则发射聚合样本（均值+峰值）进入发送队列
                if self.aggregator is not None:
                    due = self.aggregator.flush_due()
                    if due:
                        self.sender.enqueue_many(
                            [
                                s
                                for a in due
                                for s in a.to_metric_samples(
                                    emit_max=self.aggregator.emit_max
                                )
                            ]
                        )

                # 满足条件则尝试上报
                try:
                    self.sender.flush()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"上报 flush 异常: {exc}")

                # 主循环休眠时间按当前最小采集间隔动态计算：Server 下发可能
                # 运行中缩短某采集器间隔，固定休眠会让新间隔无法及时生效。
                min_interval = max(
                    1, min(c.interval for c in self.collectors)
                ) if self.collectors else 1
                time.sleep(min_interval)
        except KeyboardInterrupt:
            logger.info("收到退出信号")
        finally:
            # 关闭前发射窗口内残余聚合样本，再强制刷新发送队列，避免丢点。
            if self.aggregator is not None:
                remaining = self.aggregator.flush_all()
                if remaining:
                    self.sender.enqueue_many(
                        [
                            s
                            for a in remaining
                            for s in a.to_metric_samples(
                                emit_max=self.aggregator.emit_max
                            )
                        ]
                    )
            self.executor.stop()
            if self._executor_thread is not None:
                self._executor_thread.join(timeout=5)
            self.sender.shutdown()
            logger.info("Agent 已停止")

    def stop(self) -> None:
        """停止采集循环（供测试使用）。"""
        self.running = False


def _maybe_update_agent(config: dict, config_dir: Path) -> None:
    """根据配置检查并执行 Agent 自动/手动更新。"""
    auto_update = config.get("auto_update", {})
    if not auto_update.get("enabled", True):
        return

    server_url = config.get("server_url", "")
    agent_token = config.get("agent_token", "")
    if not server_url or not agent_token:
        return

    updater = UpdateManager(
        server_url=server_url,
        agent_token=agent_token,
        current_version=config.get("version", "0.1.0"),
        install_dir=config_dir.parent if config_dir.name == "config" else config_dir,
        config_dir=config_dir,
        channel=auto_update.get("channel", "stable"),
        mode=auto_update.get("mode", "manual"),
    )
    try:
        # 优先处理 Server 下发的手动更新任务
        updater.check_and_report_task()

        # 自动模式下再主动检查版本
        if auto_update.get("mode") == "auto":
            updater.run_update()
    finally:
        updater.close()


def main() -> None:
    """Agent 主入口。"""
    parser = argparse.ArgumentParser(description="ChaosOps Agent")
    parser.add_argument("--server-url", help="ChaosOps Server URL")
    parser.add_argument("--install-key", help="一次性安装密钥")
    parser.add_argument("--config-dir", default=".", help="配置文件保存目录")
    parser.add_argument(
        "--register-only",
        action="store_true",
        help="仅执行注册并保存配置，不启动守护模式",
    )
    parser.add_argument(
        "--skip-update-check",
        action="store_true",
        help="启动时跳过更新检查",
    )
    args = parser.parse_args()

    config_dir = Path(args.config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)

    # 注册模式：必须有 server-url 和 install-key
    if args.server_url and args.install_key:
        config = register_and_save_config(
            server_url=args.server_url.rstrip("/"),
            install_key=args.install_key,
            config_dir=config_dir,
        )
        if args.register_only:
            return
        if not args.skip_update_check:
            _maybe_update_agent(config, config_dir)
        agent = MetricAgent(config=config, config_dir=config_dir)
        agent.run()
        return

    if args.register_only:
        raise SystemExit("--register-only 需要同时提供 --server-url 和 --install-key")

    # 无注册参数时，加载本地配置进入守护模式
    config_path = config_dir / "config.json"
    config = load_config(config_path)
    if not args.skip_update_check:
        _maybe_update_agent(config, config_dir)
    agent = MetricAgent(config=config, config_dir=config_dir)
    agent.run()


if __name__ == "__main__":
    # 默认日志配置；systemd 等环境可通过配置覆盖
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    main()
