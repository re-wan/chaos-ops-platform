"""告警静默规则 API 路由。"""

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.api.deps import get_db, require_admin
from app.models.alert_silence import AlertSilence
from app.models.user import User
from app.schemas.alert_silence import AlertSilenceCreate, AlertSilenceRead
from app.services import alert_silence as silence_service

router = APIRouter(prefix="/api/v1/alert-silences", tags=["alert-silences"])


def _silence_to_read(silence: AlertSilence) -> AlertSilenceRead:
    """将 AlertSilence ORM 对象转换为响应模型。"""
    return AlertSilenceRead(
        id=silence.id,
        matchers=json.loads(silence.matchers),
        starts_at=silence.starts_at,
        ends_at=silence.ends_at,
        comment=silence.comment,
        created_by=silence.created_by,
        created_at=silence.created_at,
    )


@router.get("", response_model=list[AlertSilenceRead])
def list_alert_silences(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """列出所有静默规则（含已过期）。"""
    silences = silence_service.list_silences(db)
    return [_silence_to_read(s) for s in silences]


@router.post("", response_model=AlertSilenceRead, status_code=status.HTTP_201_CREATED)
def create_alert_silence(
    body: AlertSilenceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """创建静默规则。"""
    try:
        silence = silence_service.create_silence(
            db,
            matchers=body.matchers,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            comment=body.comment,
            created_by=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return _silence_to_read(silence)


@router.delete("/{silence_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alert_silence(
    silence_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """取消静默规则。"""
    if not silence_service.delete_silence(db, silence_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="静默规则不存在",
        )
    return None
