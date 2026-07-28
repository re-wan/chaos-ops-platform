"""通知模板 API 路由。"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from app.api.deps import get_db, get_pagination, require_admin
from app.schemas.page import Page, PageParams
from app.models.notification_template import NotificationTemplate
from app.models.user import User
from app.schemas.notification_template import (
    NotificationTemplateCreate,
    NotificationTemplatePreviewRequest,
    NotificationTemplatePreviewResponse,
    NotificationTemplateRead,
    NotificationTemplateUpdate,
)
from app.services import template_renderer

router = APIRouter(
    prefix="/api/v1/notification-templates", tags=["notification-templates"]
)


class MessageResponse(BaseModel):
    """通用消息响应。"""

    message: str


def _template_to_read(template: NotificationTemplate) -> NotificationTemplateRead:
    """将 NotificationTemplate ORM 对象转换为响应模型。"""
    return NotificationTemplateRead(
        id=template.id,
        name=template.name,
        event_type=template.event_type,
        channel_type=template.channel_type,
        subject_template=template.subject_template,
        body_template=template.body_template,
        format=template.format,
        is_default=template.is_default,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


@router.get("", response_model=Page[NotificationTemplateRead])
def list_notification_templates(
    event_type: Optional[str] = None,
    channel_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
    pagination: PageParams = Depends(get_pagination),
):
    """列出通知模板，支持 event_type/channel_type 过滤与服务端分页。"""
    filters = []
    if event_type is not None:
        filters.append(NotificationTemplate.event_type == event_type)
    if channel_type is not None:
        filters.append(NotificationTemplate.channel_type == channel_type)

    total = db.exec(
        select(func.count(NotificationTemplate.id)).where(*filters)
    ).one() or 0

    start = (pagination.page - 1) * pagination.page_size
    templates = db.exec(
        select(NotificationTemplate)
        .where(*filters)
        # id 作为 tiebreaker，保证组合键相同时翻页稳定不重复不漏
        .order_by(
            NotificationTemplate.event_type,
            NotificationTemplate.channel_type,
            NotificationTemplate.is_default,
            NotificationTemplate.id,
        )
        .offset(start)
        .limit(pagination.page_size)
    ).all()
    return Page[NotificationTemplateRead](
        items=[_template_to_read(t) for t in templates],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post(
    "", response_model=NotificationTemplateRead, status_code=status.HTTP_201_CREATED
)
def create_notification_template(
    body: NotificationTemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """创建自定义模板，覆盖同组合的默认模板。"""
    existing = db.exec(
        select(NotificationTemplate).where(
            NotificationTemplate.event_type == body.event_type,
            NotificationTemplate.channel_type == body.channel_type,
            NotificationTemplate.is_default == False,  # noqa: E712
        )
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该事件类型和渠道组合已存在自定义模板",
        )

    template = NotificationTemplate(
        name=body.name,
        event_type=body.event_type,
        channel_type=body.channel_type,
        subject_template=body.subject_template,
        body_template=body.body_template,
        format=body.format,
        is_default=False,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return _template_to_read(template)


@router.get("/{template_id}", response_model=NotificationTemplateRead)
def get_notification_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """获取模板详情。"""
    template = db.get(NotificationTemplate, template_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="模板不存在",
        )
    return _template_to_read(template)


@router.put("/{template_id}", response_model=NotificationTemplateRead)
def update_notification_template(
    template_id: int,
    body: NotificationTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """更新自定义模板。默认模板不允许通过此接口修改。"""
    template = db.get(NotificationTemplate, template_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="模板不存在",
        )
    if template.is_default:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="默认模板不可修改，请创建自定义模板覆盖",
        )

    if body.name is not None:
        template.name = body.name
    if body.subject_template is not None:
        template.subject_template = body.subject_template
    if body.body_template is not None:
        template.body_template = body.body_template
    if body.format is not None:
        template.format = body.format

    db.add(template)
    db.commit()
    db.refresh(template)
    return _template_to_read(template)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notification_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """删除自定义模板。默认模板不可删除。"""
    template = db.get(NotificationTemplate, template_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="模板不存在",
        )
    if template.is_default:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="默认模板不可删除",
        )

    db.delete(template)
    db.commit()
    return None


@router.post(
    "/{template_id}/preview", response_model=NotificationTemplatePreviewResponse
)
def preview_notification_template(
    template_id: int,
    body: NotificationTemplatePreviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """预览指定模板的渲染结果。"""
    template = db.get(NotificationTemplate, template_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="模板不存在",
        )

    result = template_renderer.preview_template(
        session=db,
        template=template,
        context=body.context,
    )
    return NotificationTemplatePreviewResponse(**result)
