"""通知渠道 API 路由。"""

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from app.api.deps import get_db, get_pagination, require_admin
from app.core.licensing import check_feature_enabled
from app.schemas.page import Page, PageParams
from app.models.user import User
from app.schemas.notification_channel import (
    NotificationChannelCreate,
    NotificationChannelRead,
    NotificationChannelTestPayload,
    NotificationChannelUpdate,
)
from app.services import audit_log
from app.services import notification as notification_service

router = APIRouter(
    prefix="/api/v1/notification-channels", tags=["notification-channels"]
)


class TestResponse(BaseModel):
    """测试通知响应。"""

    success: bool
    message: str
    log_id: Optional[int] = None


def _channel_to_read(channel) -> NotificationChannelRead:
    """将 NotificationChannel ORM 对象转换为响应模型。"""
    data = notification_service.channel_to_dict(channel, mask_sensitive=True)
    return NotificationChannelRead(**data)


_IM_CHANNEL_TYPES = {"dingtalk", "wecom", "lark", "slack"}


def _check_im_channel_feature(channel_type: str) -> None:
    """IM 渠道类型需要 License 解锁。"""
    if channel_type in _IM_CHANNEL_TYPES and not check_feature_enabled("im_channels"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前 License 未解锁 IM 通知渠道功能",
        )


@router.get("", response_model=Page[NotificationChannelRead])
def list_notification_channels(
    channel_type: Optional[str] = None,
    enabled: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
    pagination: PageParams = Depends(get_pagination),
):
    """列出通知渠道，支持服务端分页。"""
    channels = notification_service.list_channels(
        db, channel_type=channel_type, enabled=enabled
    )
    total = len(channels)
    start = (pagination.page - 1) * pagination.page_size
    end = start + pagination.page_size
    paged_channels = channels[start:end]
    return Page[NotificationChannelRead](
        items=[_channel_to_read(c) for c in paged_channels],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post(
    "", response_model=NotificationChannelRead, status_code=status.HTTP_201_CREATED
)
def create_notification_channel(
    body: NotificationChannelCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """创建通知渠道。"""
    _check_im_channel_feature(body.channel_type)
    try:
        channel = notification_service.create_channel(
            db,
            name=body.name,
            channel_type=body.channel_type,
            config=body.config,
            enabled=body.enabled,
            lang=body.lang,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    # 审计：仅成功变更落审计；底层异常隔离，失败只记日志不阻断业务。
    audit_log.record_notification_channel_created(
        db, channel, created_by=current_user.id
    )
    return _channel_to_read(channel)


@router.get("/{channel_id}", response_model=NotificationChannelRead)
def get_notification_channel(
    channel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """获取通知渠道详情。"""
    channel = notification_service.get_channel(db, channel_id)
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="通知渠道不存在",
        )
    return _channel_to_read(channel)


@router.put("/{channel_id}", response_model=NotificationChannelRead)
def update_notification_channel(
    channel_id: int,
    body: NotificationChannelUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """更新通知渠道。"""
    channel = notification_service.get_channel(db, channel_id)
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="通知渠道不存在",
        )
    if body.channel_type is not None:
        _check_im_channel_feature(body.channel_type)
    changed_fields = list(body.model_dump(exclude_unset=True).keys())
    try:
        channel = notification_service.update_channel(
            db,
            channel=channel,
            name=body.name,
            channel_type=body.channel_type,
            config=body.config,
            enabled=body.enabled,
            lang=body.lang,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    audit_log.record_notification_channel_updated(
        db, channel, changed_fields=changed_fields, created_by=current_user.id
    )
    return _channel_to_read(channel)


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notification_channel(
    channel_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """删除通知渠道。"""
    channel = notification_service.get_channel(db, channel_id)
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="通知渠道不存在",
        )
    # 删除前先留存标识字段（删除后 ORM 属性过期不可取），供审计使用。
    deleted_name = channel.name
    deleted_type = channel.channel_type
    notification_service.delete_channel(db, channel)
    audit_log.record_notification_channel_deleted(
        db,
        channel_id=channel_id,
        name=deleted_name,
        channel_type=deleted_type,
        created_by=current_user.id,
    )
    return None


@router.post("/{channel_id}/test", response_model=TestResponse)
def test_notification_channel(
    channel_id: int,
    body: NotificationChannelTestPayload = NotificationChannelTestPayload(),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """向指定渠道发送测试通知。"""
    channel = notification_service.get_channel(db, channel_id)
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="通知渠道不存在",
        )

    payload = body.payload or {}
    if channel.channel_type == "email":
        test_payload: dict[str, Any] = {
            "subject": payload.get("subject", "测试邮件"),
            "body": payload.get("body", "这是一条测试通知"),
            "to_addrs": payload.get("to_addrs", ["admin@example.com"]),
        }
    elif channel.channel_type in {
        "webhook",
        "dingtalk",
        "wecom",
        "lark",
        "slack",
    }:
        test_payload = {
            "event_type": "test",
            "event_id": str(channel_id),
            "title": payload.get("subject", "测试通知"),
            "message": payload.get("body", "这是一条测试通知"),
            "severity": "info",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的渠道类型: {channel.channel_type}",
        )

    logs = notification_service.send_notification(
        db,
        event_type="test",
        event_id=str(channel_id),
        payload=test_payload,
        channel_ids=[channel_id],
    )

    if not logs:
        return TestResponse(
            success=False,
            message="渠道已禁用或无法创建通知记录",
            log_id=None,
        )

    return TestResponse(
        success=True,
        message="测试通知已提交异步发送",
        log_id=logs[0].id,
    )
