"""告警抑制规则 API 路由。"""

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.api.deps import get_db, require_admin
from app.models.alert_inhibition import AlertInhibition
from app.models.user import User
from app.schemas.alert_inhibition import AlertInhibitionCreate, AlertInhibitionRead
from app.services import alert_inhibition as inhibition_service

router = APIRouter(prefix="/api/v1/alert-inhibitions", tags=["alert-inhibitions"])


def _inhibition_to_read(inhibition: AlertInhibition) -> AlertInhibitionRead:
    """将 AlertInhibition ORM 对象转换为响应模型。"""
    return AlertInhibitionRead(
        id=inhibition.id,
        source_matchers=json.loads(inhibition.source_matchers),
        target_matchers=json.loads(inhibition.target_matchers),
        equal_labels=json.loads(inhibition.equal_labels),
        created_at=inhibition.created_at,
    )


@router.get("", response_model=list[AlertInhibitionRead])
def list_alert_inhibitions(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """列出所有抑制规则。"""
    inhibitions = inhibition_service.list_inhibitions(db)
    return [_inhibition_to_read(i) for i in inhibitions]


@router.post(
    "", response_model=AlertInhibitionRead, status_code=status.HTTP_201_CREATED
)
def create_alert_inhibition(
    body: AlertInhibitionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """创建抑制规则。"""
    try:
        inhibition = inhibition_service.create_inhibition(
            db,
            source_matchers=body.source_matchers,
            target_matchers=body.target_matchers,
            equal_labels=body.equal_labels,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return _inhibition_to_read(inhibition)


@router.delete("/{inhibition_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alert_inhibition(
    inhibition_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """删除抑制规则。"""
    if not inhibition_service.delete_inhibition(db, inhibition_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="抑制规则不存在",
        )
    return None
