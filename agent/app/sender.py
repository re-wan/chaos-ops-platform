"""指标批量上报与本地缓存。"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from agent.app.collectors.base import MetricSample

logger = logging.getLogger("agent.sender")

# 默认配置常量
DEFAULT_BATCH_SIZE = 1000
DEFAULT_FLUSH_INTERVAL_SECONDS = 10
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_CACHE_SIZE_MB = 100
DEFAULT_MAX_CACHE_AGE_SECONDS = 3600


class MetricSender:
    """负责将 Agent 采集的指标批量上报给 Server，并在断网时缓存。"""

    def __init__(
        self,
        server_url: str,
        agent_token: str,
        node_id: str,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval_seconds: int = DEFAULT_FLUSH_INTERVAL_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cache_dir: Optional[Path] = None,
        max_cache_size_mb: int = DEFAULT_MAX_CACHE_SIZE_MB,
        max_cache_age_seconds: int = DEFAULT_MAX_CACHE_AGE_SECONDS,
        intervals_handler=None,
    ):
        self.server_url = server_url.rstrip("/")
        self.agent_token = agent_token
        self.node_id = node_id
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.cache_dir = cache_dir or Path("./metric_cache")
        self.max_cache_size_mb = max_cache_size_mb
        self.max_cache_age_seconds = max_cache_age_seconds
        # Server 下发 collector_intervals（{metric_name: 间隔秒}）时的回调，
        # 由 MetricAgent 注入；None 表示不消费该字段（兼容老 Server/测试）。
        self.intervals_handler = intervals_handler

        self.queue: list[MetricSample] = []
        self.last_flush_time = time.monotonic()
        self.headers = {"Authorization": f"Bearer {agent_token}"}

        # 确保缓存目录存在
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def enqueue(self, sample: MetricSample) -> None:
        """将单个样本加入内存队列。"""
        self.queue.append(sample)

    def enqueue_many(self, samples: list[MetricSample]) -> None:
        """将多个样本加入内存队列。"""
        self.queue.extend(samples)

    def should_flush(self) -> bool:
        """判断是否需要立即上报。"""
        if len(self.queue) >= self.batch_size:
            return True
        if time.monotonic() - self.last_flush_time >= self.flush_interval_seconds:
            return True
        return False

    def _build_payload(self, samples: list[MetricSample]) -> dict:
        """构造上报请求体。"""
        return {
            "node_id": self.node_id,
            "samples": [sample.to_dict() for sample in samples],
        }

    def _send_batch(self, samples: list[MetricSample]) -> bool:
        """尝试上报一批样本。返回是否成功。"""
        if not samples:
            return True

        url = f"{self.server_url}/api/v1/metrics/ingest"
        payload = self._build_payload(samples)

        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(url, json=payload, headers=self.headers)
                response.raise_for_status()
                result = response.json()
                logger.debug(f"上报成功: accepted={result.get('accepted')}, dropped={result.get('dropped')}")
                self._handle_server_config(result)
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"指标上报失败: {exc}")
            return False

    def _handle_server_config(self, result: dict) -> None:
        """处理 Server 响应中的配置下发字段（collector_intervals）。

        老 Server 响应无该字段时静默跳过；handler 异常只告警，
        绝不影响本次上报的成功判定（配置下发是附带能力）。
        """
        if self.intervals_handler is None:
            return
        intervals = result.get("collector_intervals")
        if not isinstance(intervals, dict) or not intervals:
            return
        try:
            self.intervals_handler(intervals)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"处理 collector_intervals 下发失败: {exc}")

    def _cache_file_path(self) -> Path:
        """生成一个新的缓存文件路径（按时间戳命名）。"""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        return self.cache_dir / f"metrics_{timestamp}.json"

    def _write_cache(self, samples: list[MetricSample]) -> None:
        """将样本写入本地缓存文件。"""
        if not samples:
            return

        self._prune_cache()

        cache_path = self._cache_file_path()
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(self._build_payload(samples), f)
            logger.info(f"已缓存 {len(samples)} 条样本到 {cache_path}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"写入缓存失败: {exc}")

    def _prune_cache(self) -> None:
        """清理超过大小/年龄上限的缓存文件。"""
        if not self.cache_dir.exists():
            return

        now = time.time()
        cache_files: list[Path] = []
        total_size = 0

        for path in sorted(self.cache_dir.glob("metrics_*.json")):
            try:
                stat = path.stat()
                age_seconds = now - stat.st_mtime
                if age_seconds > self.max_cache_age_seconds:
                    path.unlink()
                    logger.debug(f"删除过期缓存: {path}")
                    continue
                total_size += stat.st_size
                cache_files.append(path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"检查缓存文件失败 ({path}): {exc}")

        # 按大小上限清理最旧的文件
        max_size_bytes = self.max_cache_size_mb * 1024 * 1024
        if total_size > max_size_bytes:
            for path in cache_files:
                try:
                    stat = path.stat()
                    path.unlink()
                    total_size -= stat.st_size
                    logger.debug(f"清理缓存以释放空间: {path}")
                    if total_size <= max_size_bytes:
                        break
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"清理缓存文件失败 ({path}): {exc}")

    def _read_cache_files(self) -> list[Path]:
        """按文件名（即时间）排序返回所有缓存文件。"""
        if not self.cache_dir.exists():
            return []
        return sorted(self.cache_dir.glob("metrics_*.json"))

    def send_cached(self) -> bool:
        """尝试补发本地缓存中的样本。

        按时间顺序依次尝试发送；发送成功后删除缓存文件，
        失败则停止后续补发，等待下次重试。
        """
        cache_files = self._read_cache_files()
        if not cache_files:
            return True

        all_sent = True
        for path in cache_files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                samples = [
                    MetricSample(
                        metric_name=s["metric_name"],
                        value=s["value"],
                        timestamp=datetime.fromisoformat(
                            s["timestamp"].replace("Z", "+00:00")
                        ),
                        labels=s.get("labels"),
                    )
                    for s in payload.get("samples", [])
                ]

                if self._send_batch(samples):
                    path.unlink()
                    logger.info(f"缓存补发成功并删除: {path}")
                else:
                    all_sent = False
                    break
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"读取或补发缓存失败 ({path}): {exc}")
                all_sent = False
                break

        return all_sent

    def flush(self, force: bool = False) -> bool:
        """触发一次上报。

        先尝试补发缓存，再上报当前内存队列；失败时写入本地缓存。
        返回本次 flush 是否成功（内存队列是否全部送达）。
        """
        # 无论是否 force，先尝试补发缓存
        self.send_cached()

        if not force and not self.should_flush():
            return True

        if not self.queue:
            self.last_flush_time = time.monotonic()
            return True

        samples = self.queue[: self.batch_size]
        if self._send_batch(samples):
            self.queue = self.queue[self.batch_size :]
            self.last_flush_time = time.monotonic()
            # 成功后继续尝试补发缓存（可能 flush 期间又有缓存写入）
            self.send_cached()
            return True

        # 上报失败：将本次待发送样本写入缓存，并从队列移除避免重复
        self._write_cache(samples)
        self.queue = self.queue[self.batch_size :]
        self.last_flush_time = time.monotonic()
        return False

    def shutdown(self) -> None:
        """关闭前强制刷新队列。"""
        self.flush(force=True)
