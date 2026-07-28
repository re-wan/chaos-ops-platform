"""日志与数据清理任务。"""

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, select

from app.core import database as database_module
from app.core import licensing
from app.core.config import settings
from app.core.logger import get_logger
from app.core.resilience import get_database_file_path
from app.core.scheduler import safe_task
from app.models.system_setting import SystemSetting
from app.services.metrics_ingest import get_metric_backend

try:
    from app.services.optimization_lifecycle import _RETENTION_OVERRIDE_PREFIX
except ImportError:
    # 三版物理分包删除优化模块后，不存在保留期覆盖键，跳过覆盖清理。
    _RETENTION_OVERRIDE_PREFIX = None


logger = get_logger("tasks.cleanup")

# 磁盘使用告警阈值
_STORAGE_USAGE_ALERT_PERCENT = 85.0


@safe_task
def cleanup_task() -> None:
    """定时执行清理任务。

    MVP 阶段主要清理：
        - 7 天前的备份文件（保留数量策略已处理，此处做时间兜底）
        - 过期的 Session Token（未来扩展）
        - 超过保留期的 InfluxDB 指标数据
        - 磁盘空间使用告警
    """
    logger.info("开始执行定时清理任务")

    # 清理超过 7 天的备份
    backup_dir = Path(settings.BACKUP_DIR)
    if backup_dir.exists():
        cutoff = datetime.now() - timedelta(days=7)
        for backup_file in backup_dir.glob("chaosops_*.db"):
            try:
                mtime = datetime.fromtimestamp(backup_file.stat().st_mtime)
                if mtime < cutoff:
                    backup_file.unlink()
                    logger.info(f"删除过期备份: {backup_file}")
            except OSError as e:
                logger.warning(f"清理备份文件失败 {backup_file}: {e}")

    # 数据库 VACUUM（仅 SQLite）
    db_path = get_database_file_path()
    if db_path is not None and db_path.exists():
        import sqlite3

        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute("VACUUM")
            conn.close()
            logger.info("数据库 VACUUM 完成")
        except Exception as e:
            logger.error(f"数据库 VACUUM 失败: {e}")

    # 清理超过保留期的指标数据
    cleanup_metric_retention()

    # 磁盘空间告警
    check_storage_usage()

    logger.info("定时清理任务完成")


def _load_retention_overrides() -> list[tuple[str, str, float]]:
    """读取所有 ``metric_retention_override:*`` 覆盖键。

    返回 (node_id, metric_name, days) 列表；格式或值非法的键跳过并告警，
    读取失败时返回空列表（不阻断全局清理）。
    """
    overrides: list[tuple[str, str, float]] = []
    if _RETENTION_OVERRIDE_PREFIX is None:
        return overrides
    try:
        with Session(database_module.engine) as session:
            rows = session.exec(
                select(SystemSetting).where(
                    SystemSetting.key.startswith(_RETENTION_OVERRIDE_PREFIX)
                )
            ).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取保留期覆盖键失败，跳过覆盖清理: {exc}")
        return overrides

    for row in rows:
        rest = row.key[len(_RETENTION_OVERRIDE_PREFIX):]
        node_id, sep, metric_name = rest.partition(":")
        if not sep or not node_id or not metric_name:
            logger.warning(f"忽略格式非法的保留期覆盖键: {row.key}")
            continue
        try:
            days = float(row.value)
        except ValueError:
            logger.warning(f"忽略值非法的保留期覆盖键: {row.key}={row.value!r}")
            continue
        if days <= 0:
            logger.warning(f"忽略非正保留期的覆盖键: {row.key}={row.value!r}")
            continue
        overrides.append((node_id, metric_name, days))
    return overrides


# License 读取异常/矩阵缺值时的兜底保留期（免费版口径，fail-closed 只缩短）
_FALLBACK_LICENSE_RETENTION_DAYS = 7


def _license_retention_days() -> int:
    """返回当前 License 允许的最大指标保留天数。

    fail-closed：License 读取异常或矩阵值非法时按免费版 7 天处理
    （与覆盖键 min 语义一致：License 只能缩短保留期，不能延长）。
    """
    try:
        value = licensing.get_license_features().get("metric_retention_days")
        if not isinstance(value, (int, float)) or value <= 0:
            return _FALLBACK_LICENSE_RETENTION_DAYS
        return int(value)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取 License 保留期失败，按免费版 7 天处理: {exc}")
        return _FALLBACK_LICENSE_RETENTION_DAYS


@safe_task
def cleanup_metric_retention() -> None:
    """删除超过保留期的指标数据。

    保留期 = min(settings.INFLUXDB_RETENTION_DAYS, License metric_retention_days)
    （fail-closed：License 只能缩短保留期，语义与 retention 覆盖键一致）；
    License 变更后下次清理周期（次日）生效，不追溯。
    优化建议应用后写入的 ``metric_retention_override:{node_id}:{metric_name}``
    覆盖键同样采用“覆盖只能缩短”语义：有效保留期 = min(覆盖值, 全局值)。
    该语义下清理顺序无关，全局删除无需排除覆盖对；
    如需延长某指标的保留期，请调大全局 INFLUXDB_RETENTION_DAYS 并升级 License。
    """
    configured_days = settings.INFLUXDB_RETENTION_DAYS
    if configured_days <= 0:
        logger.info("保留期设置为 0，跳过指标清理")
        return
    license_days = _license_retention_days()
    retention_days = min(configured_days, license_days)
    if retention_days < configured_days:
        logger.info(
            f"License 保留期 {license_days} 天短于配置 {configured_days} 天，"
            f"按 {retention_days} 天执行清理（fail-closed 只缩短）"
        )

    now = datetime.now(timezone.utc)
    try:
        backend = get_metric_backend()
    except Exception as e:  # noqa: BLE001
        logger.error(f"获取指标后端失败，跳过指标清理: {e}")
        return

    # 先处理 per-指标保留期覆盖（min 语义下覆盖期 ≤ 全局期，顺序无关）
    for node_id, metric_name, days in _load_retention_overrides():
        effective_days = min(days, float(retention_days))
        cutoff = now - timedelta(days=effective_days)
        try:
            deleted = backend.delete_before(
                cutoff, node_id=node_id, metric_name=metric_name
            )
            logger.info(
                f"覆盖清理完成: {node_id}/{metric_name} "
                f"有效保留期 {effective_days} 天，删除条数: {deleted}"
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"覆盖清理失败 {node_id}/{metric_name}: {e}")

    # 再跑全局删除
    cutoff = now - timedelta(days=retention_days)
    logger.info(f"清理 {cutoff.isoformat()} 之前的指标数据（保留期 {retention_days} 天）")

    try:
        deleted = backend.delete_before(cutoff)
        logger.info(f"指标清理完成，删除条数: {deleted}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"指标保留期清理失败: {e}")


@safe_task
def check_storage_usage() -> None:
    """检查关键目录磁盘使用率，超过阈值时记录告警日志。

    事件中心告警能力在 Step 15/16 实现，当前仅记录日志。
    """
    paths_to_check = []

    db_path = get_database_file_path()
    if db_path is not None:
        paths_to_check.append(db_path.parent)

    backup_dir = Path(settings.BACKUP_DIR)
    if backup_dir.exists():
        paths_to_check.append(backup_dir)

    log_dir = Path(settings.LOG_DIR)
    if log_dir.exists():
        paths_to_check.append(log_dir)

    checked = set()
    for path in paths_to_check:
        try:
            resolved = path.resolve()
            if resolved in checked:
                continue
            checked.add(resolved)

            usage = shutil.disk_usage(resolved)
            percent = (usage.used / usage.total) * 100.0
            if percent >= _STORAGE_USAGE_ALERT_PERCENT:
                logger.warning(
                    f"磁盘使用率超过 {_STORAGE_USAGE_ALERT_PERCENT}%: "
                    f"{resolved} 使用 {percent:.1f}%"
                )
        except Exception as e:
            logger.warning(f"检查磁盘使用率失败 ({path}): {e}")
