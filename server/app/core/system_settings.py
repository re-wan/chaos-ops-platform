"""系统运行时配置读写服务。

提供对 `system_settings` 表的类型安全封装，
支持环境变量默认值与运行时持久化覆盖。
"""

from typing import Optional

from sqlmodel import Session, select

from app.core.config import settings
from app.core import database as database_module
from app.core.logger import get_logger
from app.core.utils import now_utc
from app.models.system_setting import SystemSetting

logger = get_logger("core.system_settings")

# 指标后端类型设置键
METRIC_BACKEND_TYPE_KEY = "metric_backend_type"

# 合法的后端类型
VALID_BACKEND_TYPES = {"influxdb", "sqlite"}


def get_setting(key: str) -> Optional[str]:
    """读取指定 key 的系统设置值，不存在返回 None。"""
    try:
        with Session(database_module.engine) as session:
            row = session.get(SystemSetting, key)
            return row.value if row else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取系统设置失败 key={key}: {exc}")
        return None


def set_setting(key: str, value: str) -> None:
    """写入或更新指定 key 的系统设置值。"""
    try:
        with Session(database_module.engine) as session:
            row = session.get(SystemSetting, key)
            now = now_utc()
            if row is None:
                row = SystemSetting(key=key, value=value, updated_at=now)
                session.add(row)
            else:
                row.value = value
                row.updated_at = now
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"写入系统设置失败 key={key}: {exc}")
        raise


def list_settings_by_prefix(prefix: str) -> dict[str, str]:
    """按键前缀列举系统设置，返回 {key: value} 字典。

    用于 check_interval_override:{node_id}:* 这类成组覆盖键的读取，
    避免业务代码裸写 SQL。LIKE 前缀匹配；失败时返回空字典并告警，
    保证调用方（如 ingest 热路径）不因配置读取异常而中断。
    """
    if not prefix:
        return {}
    try:
        with Session(database_module.engine) as session:
            rows = session.exec(
                select(SystemSetting).where(SystemSetting.key.like(f"{prefix}%"))
            ).all()
            # LIKE 中 ``_``/``%`` 是通配符（node_id 常含下划线），用 startswith
            # 二次过滤保证精确前缀匹配，避免通配误中相邻节点/指标的键。
            return {row.key: row.value for row in rows if row.key.startswith(prefix)}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"按前缀列举系统设置失败 prefix={prefix}: {exc}")
        return {}


def get_metric_backend_type() -> str:
    """获取当前配置的指标后端类型。

    优先级：
        1. system_settings 表中持久化的值
        2. 环境变量 / .env 中的 METRIC_BACKEND_TYPE 默认值

    返回 "influxdb" 或 "sqlite"，非法值回退到 "influxdb"。
    """
    persisted = get_setting(METRIC_BACKEND_TYPE_KEY)
    candidate = persisted if persisted is not None else settings.METRIC_BACKEND_TYPE
    candidate = candidate.lower().strip()
    if candidate not in VALID_BACKEND_TYPES:
        logger.warning(f"非法指标后端类型 '{candidate}'，回退到 influxdb")
        return "influxdb"
    return candidate


def set_metric_backend_type(backend_type: str) -> None:
    """持久化指标后端类型。"""
    backend_type = backend_type.lower().strip()
    if backend_type not in VALID_BACKEND_TYPES:
        raise ValueError(f"非法指标后端类型: {backend_type}，仅支持 influxdb/sqlite")
    set_setting(METRIC_BACKEND_TYPE_KEY, backend_type)
