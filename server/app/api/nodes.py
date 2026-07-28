"""节点管理 API 路由。"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core.error_messages import error_detail, error_detail_from_exception
from app.api.deps import get_current_user, get_db, get_pagination, require_admin, require_feature
from app.core.utils import resolve_public_server_url
from app.models.node import Node
from app.models.user import User
from app.schemas.node import (
    BulkNodeCreateRequest,
    BulkNodeCreateResponse,
    BulkNodeResultItem,
    NodeCreate,
    NodeInstallInfo,
    NodeRead,
    NodeUpdate,
)
from app.schemas.page import Page, PageParams
from app.api.agent_version import AgentUpdateTaskRead, create_update_task_for_node
from app.services import node_service

try:
    from app.services import bulk_registration
except ImportError:
    # 三版物理分包删除批量注册服务后，批量端点在函数体入口返回 404。
    bulk_registration = None



router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])


class _BulkErrorDetail(BaseModel):
    """批量接口错误明细。"""

    detail: str


def _node_to_read(node: Node) -> NodeRead:
    """将 Node ORM 对象转换为 NodeRead 响应模型。"""
    return NodeRead(
        id=node.id,
        node_id=node.node_id,
        name=node.name,
        host=node.host,
        description=node.description,
        platform=node.platform,
        group=node.group,
        labels=node_service._parse_labels(node.labels),
        status=node.status,
        last_seen=node.last_seen,
        is_local=node.is_local,
        is_deleted=node.is_deleted,
        created_at=node.created_at,
        updated_at=node.updated_at,
    )


def _task_to_response(task) -> BulkNodeCreateResponse:
    """将 BulkRegistrationTask 转换为响应模型。"""
    import json

    details = json.loads(task.details)
    results = [
        BulkNodeResultItem(
            name=item.get("name") or "",
            node_id=item.get("node_id"),
            status=item.get("status", "failure"),
            error=item.get("error"),
        )
        for item in details
    ]
    return BulkNodeCreateResponse(
        task_id=task.id,
        total_count=task.total_count,
        success_count=task.success_count,
        failure_count=task.failure_count,
        results=results,
        install_script_url=f"/api/v1/nodes/batch/{task.id}/install-script",
    )


@router.post("", response_model=NodeRead, status_code=status.HTTP_201_CREATED)
def create_node(
    request: Request,
    body: NodeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """创建新节点。"""
    try:
        node = node_service.create_node(
            db,
            name=body.name,
            host=body.host,
            description=body.description,
            platform=body.platform,
            labels=body.labels,
            group=body.group,
        )
    except ValueError as e:
        detail = error_detail_from_exception(request, e)
        if "已存在" in str(e):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    except IntegrityError:
        # 兜底：并发创建等场景绕过应用层预检后由 DB 唯一约束拦截（如部分唯一索引
        # uq_nodes_name_active），此时 session 已不可用，必须 rollback 再返回 400
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "节点名称已存在（唯一约束冲突）"),
        )

    return _node_to_read(node)


@router.get("", response_model=Page[NodeRead])
def list_nodes(
    group: Optional[str] = None,
    label_key: Optional[str] = None,
    label_value: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    pagination: PageParams = Depends(get_pagination),
):
    """列出所有未删除节点，支持按分组、标签筛选与服务端分页。"""
    paged_nodes, total = node_service.list_nodes_paged(
        db,
        group=group,
        label_key=label_key,
        label_value=label_value,
        page=pagination.page,
        page_size=pagination.page_size,
    )
    return Page[NodeRead](
        items=[_node_to_read(n) for n in paged_nodes],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("/batch", response_model=BulkNodeCreateResponse, status_code=status.HTTP_201_CREATED)
def batch_create_nodes(
    request: Request,
    body: BulkNodeCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
    _=Depends(require_feature("bulk_registration")),
):
    """JSON 批量创建节点。"""
    if bulk_registration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "nodes.bulk_not_available"),
        )
    try:
        task = bulk_registration.bulk_create_nodes(
            db,
            items=body.nodes,
            created_by=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_detail_from_exception(request, e)) from e
    return _task_to_response(task)


@router.post("/batch/csv", response_model=BulkNodeCreateResponse, status_code=status.HTTP_201_CREATED)
def batch_create_nodes_csv(
    request: Request,
    file: UploadFile,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
    _=Depends(require_feature("bulk_registration")),
):
    """CSV 文件上传批量创建节点。"""
    if bulk_registration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "nodes.bulk_not_available"),
        )
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "nodes.csv_required"),
        )

    try:
        content = file.file.read()
        items = bulk_registration.parse_csv_file(content)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_detail_from_exception(request, e)) from e
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "nodes.csv_encoding"),
        )
    finally:
        file.file.close()

    try:
        task = bulk_registration.bulk_create_nodes(
            db,
            items=items,
            created_by=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_detail_from_exception(request, e)) from e
    return _task_to_response(task)


@router.get("/batch/{task_id}", response_model=BulkNodeCreateResponse)
def get_bulk_task(
    request: Request,
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_feature("bulk_registration")),
):
    """查询批量任务结果。"""
    if bulk_registration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "nodes.bulk_not_available"),
        )
    task = bulk_registration.get_bulk_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail(request, "nodes.bulk_task_not_found"))
    return _task_to_response(task)


@router.get("/batch/{task_id}/install-script")
def download_batch_install_script(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _=Depends(require_feature("bulk_registration")),
):
    """下载批量部署 bash 脚本。"""
    if bulk_registration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "nodes.bulk_not_available"),
        )
    task = bulk_registration.get_bulk_task(db, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail(request, "nodes.bulk_task_not_found"))

    try:
        server_url = resolve_public_server_url(request)
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "nodes.public_url_missing_script"),
        )

    script = bulk_registration.generate_batch_install_script(db, task, server_url)

    from fastapi.responses import PlainTextResponse

    filename = f"chaosops-batch-install-{task_id}.sh"
    return PlainTextResponse(
        script,
        media_type="text/x-shellscript",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/{node_id}", response_model=NodeRead)
def get_node(
    request: Request,
    node_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取节点详情。"""
    node = node_service.get_node_by_id(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail(request, "nodes.not_found"))
    return _node_to_read(node)


@router.put("/{node_id}", response_model=NodeRead)
def update_node(
    request: Request,
    node_id: int,
    body: NodeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """更新节点信息。"""
    node = node_service.get_node_by_id(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail(request, "nodes.not_found"))

    try:
        node = node_service.update_node(
            db,
            node,
            name=body.name,
            host=body.host,
            description=body.description,
            platform=body.platform,
            labels=body.labels,
            group=body.group,
        )
    except ValueError as e:
        detail = error_detail_from_exception(request, e)
        if "已存在" in str(e):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)

    return _node_to_read(node)


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_node(
    request: Request,
    node_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """软删除节点。"""
    node = node_service.get_node_by_id(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail(request, "nodes.not_found"))

    try:
        node_service.delete_node(db, node)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_detail_from_exception(request, e))

    return None


@router.post("/{node_id}/reset-token", response_model=NodeRead)
def reset_node_token(
    request: Request,
    node_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """重置 Agent Token 和 Install Key，使旧 Token 立即失效。"""
    node = node_service.get_node_by_id(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail(request, "nodes.not_found"))

    try:
        node = node_service.reset_node_token_service(db, node)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_detail_from_exception(request, e))

    return _node_to_read(node)


@router.post("/{node_id}/revoke-token", response_model=NodeRead)
def revoke_node_token(
    request: Request,
    node_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """撤销当前 Agent Token（不生成新 Token），用于 Token 泄露紧急处理。"""
    node = node_service.get_node_by_id(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail(request, "nodes.not_found"))

    if node.is_local:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "nodes.local_token_revoke_forbidden"),
        )

    node = node_service.revoke_node_token_service(db, node)
    return _node_to_read(node)


@router.post("/{node_id}/regenerate-install-key", response_model=NodeRead)
def regenerate_install_key(
    request: Request,
    node_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """重新生成 Install Key。"""
    node = node_service.get_node_by_id(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail(request, "nodes.not_found"))

    node = node_service.regenerate_install_key_service(db, node)
    return _node_to_read(node)


@router.get("/{node_id}/install", response_model=NodeInstallInfo)
def get_install_command(
    node_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """获取节点一键安装命令。"""
    node = node_service.get_node_by_id(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail(request, "nodes.not_found"))

    try:
        server_url = resolve_public_server_url(request)
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "nodes.public_url_missing_cmd"),
        )

    commands = node_service.generate_install_commands(node, server_url)
    return NodeInstallInfo(**commands)


@router.post("/{node_id}/update", response_model=AgentUpdateTaskRead, status_code=status.HTTP_201_CREATED)
def trigger_node_update(
    request: Request,
    node_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """手动触发节点 Agent 更新，创建更新任务。"""
    node = node_service.get_node_by_id(db, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_detail(request, "nodes.not_found"))

    try:
        task = create_update_task_for_node(db, node)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_detail_from_exception(request, e)) from e

    return AgentUpdateTaskRead(
        task_id=task.task_id,
        node_id=task.node_id,
        target_version=task.target_version,
        status=task.status,
        error_message=task.error_message,
        result=task.result,
        created_at=task.created_at,
    )

