"""License 管理 API 路由。"""

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.api.deps import require_admin
from app.core.config import settings
from app.core import licensing
from app.models.license import LicenseInfo

router = APIRouter(prefix="/api/v1/license", tags=["license"])


class LicenseRead(BaseModel):
    """脱敏后的 License 信息响应。"""

    license_id: str
    edition: str
    max_nodes: int
    issued_at: str | None
    expires_at: str | None
    server_install_id: str | None
    is_valid: bool
    error_message: str | None


class FeaturesRead(BaseModel):
    """功能解锁矩阵响应。"""

    edition: str
    features: dict[str, Any]


class UploadResponse(BaseModel):
    """License 上传响应。"""

    success: bool
    message: str
    license: LicenseRead


def _license_to_read(license_info: LicenseInfo) -> LicenseRead:
    """将 LicenseInfo 转换为脱敏响应模型。"""
    return LicenseRead(
        license_id=license_info.license_id,
        edition=license_info.edition,
        max_nodes=license_info.max_nodes,
        issued_at=license_info.issued_at.isoformat() if license_info.issued_at else None,
        expires_at=license_info.expires_at.isoformat() if license_info.expires_at else None,
        server_install_id=license_info.server_install_id,
        is_valid=license_info.is_valid,
        error_message=license_info.error_message,
    )


@router.get("", response_model=LicenseRead)
def get_license():
    """获取当前 License 信息（已脱敏）。"""
    return _license_to_read(licensing.get_cached_license())


@router.get("/features", response_model=FeaturesRead)
def get_features():
    """获取当前版本解锁的功能矩阵。"""
    license_info = licensing.get_cached_license()
    return FeaturesRead(
        edition=licensing._effective_edition(license_info),
        features=licensing.get_license_features(license_info),
    )


@router.post("/upload", response_model=UploadResponse)
def upload_license(
    file: UploadFile,
    _: dict = Depends(require_admin),
):
    """上传新的 License 文件（仅 admin）。"""
    if not file.filename or not file.filename.lower().endswith(".json"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请上传 JSON 格式的 License 文件",
        )

    try:
        content = file.file.read()
        data = json.loads(content.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"License 文件不是合法 JSON: {e}",
        ) from e
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"License 文件编码错误: {e}",
        ) from e
    finally:
        file.file.close()

    # 先校验 License 是否合法，不合法则不保存
    temp_info = licensing._validate_license(data)
    if not temp_info.is_valid and temp_info.error_message != "License 已过期":
        # 过期 License 允许保存，但会进入限制模式
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=temp_info.error_message or "License 校验失败",
        )

    try:
        with open(settings.LICENSE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"保存 License 文件失败: {e}",
        ) from e

    new_info = licensing.refresh_license()
    return UploadResponse(
        success=True,
        message="License 已上传并生效",
        license=_license_to_read(new_info),
    )
