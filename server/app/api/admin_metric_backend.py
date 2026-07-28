"""指标后端管理 API。

提供当前后端状态查看、连接测试、运行时切换与历史数据迁移接口。
所有变更操作仅 admin 可执行，并记录审计日志。
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.api.deps import get_db, require_admin
from app.core.config import settings
from app.core.logger import get_logger
from app.core.metric_backend import MetricBackend
from app.core.system_settings import get_metric_backend_type
from app.services import audit_log
from app.services.metrics_ingest import (
    _create_backend,
    get_metric_backend,
    switch_metric_backend,
)
from app.services.metric_migration import migrate_between_backends

logger = get_logger("api.admin_metric_backend")

router = APIRouter(prefix="/api/v1/admin/metric-backend", tags=["admin-metric-backend"])


class BackendTypeRequest(BaseModel):
    """指定后端类型的请求体。"""

    backend_type: str = Field(..., description="后端类型：influxdb / sqlite")
    config: Optional[dict] = Field(default=None, description="可选的配置覆盖")


class SwitchRequest(BaseModel):
    """切换后端请求体。"""

    backend_type: str = Field(..., description="目标后端类型：influxdb / sqlite")


class MigrateRequest(BaseModel):
    """迁移数据请求体。"""

    source: str = Field(..., description="源后端类型：influxdb / sqlite")
    target: str = Field(..., description="目标后端类型：influxdb / sqlite")
    source_config: Optional[dict] = Field(default=None, description="源后端可选配置覆盖")
    target_config: Optional[dict] = Field(default=None, description="目标后端可选配置覆盖")


class BackendStatusResponse(BaseModel):
    """后端状态响应。"""

    current_backend: str
    healthy: bool
    config: dict


class TestResponse(BaseModel):
    """连接测试结果响应。"""

    success: bool
    backend_type: str
    healthy: bool
    message: str


class SwitchResponse(BaseModel):
    """切换后端响应。"""

    success: bool
    current_backend: str


class MigrateResponse(BaseModel):
    """迁移数据响应。"""

    success: bool
    migrated_count: int


def _get_backend_status() -> dict:
    """组装当前后端状态与配置。"""
    backend_type = get_metric_backend_type()
    backend = get_metric_backend()
    healthy = backend.check_health() if backend else False

    sqlite_url = settings.METRIC_SQLITE_DATABASE_URL or settings.DATABASE_URL
    # 生产环境中避免在 API 中暴露完整 Token
    influxdb_token_masked = ""
    if settings.INFLUXDB_TOKEN and len(settings.INFLUXDB_TOKEN) > 8:
        influxdb_token_masked = settings.INFLUXDB_TOKEN[:4] + "****" + settings.INFLUXDB_TOKEN[-4:]

    return {
        "current_backend": backend_type,
        "healthy": healthy,
        "config": {
            "influxdb_url": settings.INFLUXDB_URL,
            "influxdb_org": settings.INFLUXDB_ORG,
            "influxdb_bucket": settings.INFLUXDB_BUCKET,
            "influxdb_token": influxdb_token_masked,
            "metric_sqlite_database_url": sqlite_url,
        },
    }


@router.get("", response_model=BackendStatusResponse)
def get_backend_status(
    current_user=Depends(require_admin),
) -> dict:
    """获取当前指标后端状态与配置。"""
    return _get_backend_status()


@router.post("/test", response_model=TestResponse)
def test_backend_connection(
    request: BackendTypeRequest,
    current_user=Depends(require_admin),
) -> dict:
    """测试指定后端连接（支持临时配置覆盖，但不保存）。"""
    backend_type = request.backend_type.lower().strip()
    if backend_type not in {"influxdb", "sqlite"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"非法后端类型: {backend_type}",
        )

    try:
        backend: MetricBackend = _create_backend(backend_type, request.config)
        healthy = backend.check_health()
        backend.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"测试 {backend_type} 连接失败: {exc}")
        return {
            "success": False,
            "backend_type": backend_type,
            "healthy": False,
            "message": str(exc),
        }

    return {
        "success": healthy,
        "backend_type": backend_type,
        "healthy": healthy,
        "message": "连接正常" if healthy else "连接失败",
    }


@router.post("/switch", response_model=SwitchResponse)
def switch_backend(
    request: SwitchRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
) -> dict:
    """切换当前指标后端。

    切换前会自动测试目标后端连接，失败返回 400。
    """
    backend_type = request.backend_type.lower().strip()
    if backend_type not in {"influxdb", "sqlite"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"非法后端类型: {backend_type}",
        )

    # 先测试目标后端连接
    test_backend = _create_backend(backend_type)
    if not test_backend.check_health():
        test_backend.close()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"目标后端 {backend_type} 连接测试失败，无法切换",
        )
    test_backend.close()

    try:
        switch_metric_backend(backend_type, test_connection=False)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"切换指标后端失败: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="切换指标后端失败",
        )

    audit_log.record_metric_backend_switched(
        db,
        backend_type=backend_type,
        created_by=current_user.id,
    )

    return {
        "success": True,
        "current_backend": get_metric_backend_type(),
    }


@router.post("/migrate", response_model=MigrateResponse)
def migrate_backend_data(
    request: MigrateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin),
) -> dict:
    """触发历史数据迁移。

    在源后端与目标后端之间双向迁移，已存在数据不重复导入。
    迁移过程不影响当前活跃后端的实时写入。
    """
    source = request.source.lower().strip()
    target = request.target.lower().strip()

    if source not in {"influxdb", "sqlite"} or target not in {"influxdb", "sqlite"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="源后端与目标后端必须是 influxdb 或 sqlite",
        )

    if source == target:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="源后端与目标后端不能相同",
        )

    try:
        count = migrate_between_backends(
            source,
            target,
            source_config=request.source_config,
            target_config=request.target_config,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"指标数据迁移失败: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"迁移失败: {exc}",
        )

    audit_log.record_metric_migration(
        db,
        source=source,
        target=target,
        count=count,
        created_by=current_user.id,
    )

    return {
        "success": True,
        "migrated_count": count,
    }
