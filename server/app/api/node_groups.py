"""节点组 API 路由。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from app.api.deps import get_current_user, get_db, require_admin
from app.models.node_group import NodeGroup
from app.models.user import User
from app.schemas.node_group import NodeGroupCreate, NodeGroupRead, NodeGroupUpdate
from app.services import node_group as node_group_service

router = APIRouter(prefix="/api/v1/node-groups", tags=["node-groups"])

def _group_to_read(group: NodeGroup) -> NodeGroupRead:
    """将 NodeGroup ORM 对象转换为响应模型。"""
    return NodeGroupRead(
        id=group.id,
        name=group.name,
        description=group.description,
        node_ids=node_group_service._parse_node_ids(group.node_ids),
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


@router.get("", response_model=list[NodeGroupRead])
def list_node_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列出所有节点组。"""
    groups = node_group_service.list_node_groups(db)
    return [_group_to_read(g) for g in groups]


@router.post("", response_model=NodeGroupRead, status_code=status.HTTP_201_CREATED)
def create_node_group(
    body: NodeGroupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """创建节点组（admin）。"""
    try:
        group = node_group_service.create_node_group(
            db,
            name=body.name,
            description=body.description,
            node_ids=body.node_ids,
        )
    except ValueError as e:
        detail = str(e)
        if "已存在" in detail:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
        )

    return _group_to_read(group)


@router.get("/{group_id}", response_model=NodeGroupRead)
def get_node_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取节点组详情。"""
    group = node_group_service.get_node_group_by_id(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点组不存在",
        )
    return _group_to_read(group)


@router.put("/{group_id}", response_model=NodeGroupRead)
def update_node_group(
    group_id: int,
    body: NodeGroupUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """更新节点组（admin）。"""
    group = node_group_service.get_node_group_by_id(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点组不存在",
        )

    try:
        group = node_group_service.update_node_group(
            db,
            group,
            **body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        detail = str(e)
        if "已存在" in detail:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
        )

    return _group_to_read(group)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_node_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """删除节点组（admin）。"""
    group = node_group_service.get_node_group_by_id(db, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="节点组不存在",
        )

    node_group_service.delete_node_group(db, group)
    return None
