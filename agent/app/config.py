"""Agent 本地配置读写与默认值。"""

import json
import os
import platform
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG_PATHS = [
    "/opt/chaosops-agent/config.json",
    "C:\\ProgramData\\ChaosOpsAgent\\config.json",
    "./config.json",
]

# 默认采集与上报配置
DEFAULT_COLLECTOR_CONFIG = {
    "collectors": {
        "host": {"enabled": True, "interval": 15},
        "network": {"enabled": True, "interval": 15},
        "http": {
            "enabled": True,
            "interval": 30,
            "targets": ["http://localhost:8000/health"],
            "timeout": 10.0,
        },
        "process": {
            "enabled": True,
            "interval": 15,
            "top_n": 10,
        },
        "load": {"enabled": True, "interval": 15},
        "diskio": {"enabled": True, "interval": 15},
        "service": {
            "enabled": True,
            "interval": 30,
            # 要监控的 systemd 服务名列表；默认为空（不采集任何服务）
            "services": [],
            "timeout_seconds": 3.0,
        },
        "netstat": {"enabled": True, "interval": 15},
    },
    "batch": {
        "max_samples": 1000,
        "flush_interval_seconds": 10,
    },
    "aggregation": {
        "enabled": True,
        "window_seconds": 10,
        "max_points_per_key": 10000,
        # 批 14：额外发射 {metric_name}_max 峰值样本（峰值告警场景）；
        # 对老 Server 无破坏性（视为普通新指标），置 False 退回仅发均值。
        "emit_max": True,
    },
    "buffer": {
        "max_size_mb": 100,
        "max_age_seconds": 3600,
    },
    "sender": {
        "timeout_seconds": 10.0,
    },
    "auto_update": {
        "enabled": True,
        "mode": "manual",
        "check_interval_hours": 24,
        "channel": "stable",
    },
    "custom_script": {
        "enabled": True,
        "timeout_seconds": 60,
        "max_memory_mb": 256,
        "max_cpu_seconds": 30,
        "max_file_size_mb": 64,
        "max_processes": 64,
        "work_dir": "/tmp/chaosops-sandbox" if platform.system() != "Windows" else "C:\\Temp\\chaosops-sandbox",
        "blocked_patterns": [
            # rm -rf / 及其常见变体
            r"\brm\s+-rf\s+/",
            r"\b(/bin/rm|/usr/bin/rm)\s+-rf\s+/",
            # 格式化/分区/危险 dd
            r"\bmkfs\.?\w*\b",
            r"\bfdisk\b",
            r"\bdd\s+if\s*=\s*/dev",
            # 写入敏感系统路径（重定向或 dd）
            r">\s*/(etc|bin|sbin|usr/bin|usr/sbin|dev|sys|proc|boot|lib|lib64)(/|$)",
            r"\bdd\s+.*\bof\s*=\s*/(dev|sys|proc|boot|etc)(/|$)",
            # 关机/重启/系统控制类命令（要求作为命令出现，避免误伤 echo reboot）
            r"(?:^|[;|&`$()])\s*(shutdown|reboot|halt|poweroff|init\s+\d|systemctl|journalctl)\b",
            # 强杀命令
            r"\bkill\s+-9\b",
            r"\bkillall\b",
            r"\bpkill\b",
            # fork 炸弹
            r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        ],
        "allowed_env_vars": [
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "TZ",
            "USER",
            "SHELL",
            "TERM",
        ],
        "max_output_bytes": 50 * 1024,
        # 批 15：可选沙箱层。blacklist=默认黑名单沙箱（行为不变）；docker=容器级
        # 隔离（断网/只读 FS/资源限额），需本机 docker + 预置镜像，探测失败自动
        # 降级 blacklist（30s 节流告警），见 actions/docker_runner.py。
        "sandbox_runner": "blacklist",
        "docker_image": "python:3.12-slim",
        "docker_cpus": 1.0,
        "docker_pids_limit": 64,
    },
}


def find_config_path() -> Optional[Path]:
    """查找 Agent 配置文件路径。"""
    env_path = os.getenv("CHAOSOPS_AGENT_CONFIG")
    if env_path:
        return Path(env_path)
    for p in DEFAULT_CONFIG_PATHS:
        path = Path(p)
        if path.exists():
            return path
    return None


def load_config(config_path: Optional[Path] = None) -> dict:
    """加载 Agent 配置文件。"""
    path = config_path or find_config_path()
    if path is None:
        raise FileNotFoundError("未找到 Agent 配置文件")
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_config(config: dict, config_path: Path) -> None:
    """保存 Agent 配置文件（原子写：临时文件 + rename）。

    采集间隔覆盖等运行期配置会持续回写此文件；原子写保证进程崩溃时
    配置文件要么完整更新、要么保持旧版本，不会留下半截 JSON。
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_name(config_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, config_path)


def get_collector_config(config: Optional[dict] = None) -> dict:
    """获取采集器配置，缺失字段使用默认值填充。"""
    base = DEFAULT_COLLECTOR_CONFIG.copy()
    if config is None:
        return base

    user = config.get("collectors", {})
    for name, default in base["collectors"].items():
        if name in user:
            # 用默认值兜底缺失字段
            merged = default.copy()
            merged.update(user[name])
            base["collectors"][name] = merged

    if "batch" in config:
        base["batch"].update(config["batch"])
    if "aggregation" in config:
        base["aggregation"].update(config["aggregation"])
    if "buffer" in config:
        base["buffer"].update(config["buffer"])
    if "sender" in config:
        base["sender"].update(config["sender"])
    if "auto_update" in config:
        base["auto_update"].update(config["auto_update"])

    return base


def get_custom_script_config(config: Optional[dict] = None) -> dict:
    """获取自定义脚本沙箱配置，缺失字段使用默认值填充。"""
    base = DEFAULT_COLLECTOR_CONFIG["custom_script"].copy()
    if config is None:
        return base

    user = config.get("custom_script", {})
    for key, default in base.items():
        if key in user:
            base[key] = user[key]

    return base
