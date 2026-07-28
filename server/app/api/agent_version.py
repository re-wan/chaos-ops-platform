"""Agent 版本管理与更新任务 API 路由。"""

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlmodel import Session, desc, select

from app.core.error_messages import error_detail
from app.api.deps import get_current_node, get_db, require_admin
from app.core.logger import get_logger
from app.core.utils import ensure_utc, now_utc, resolve_public_server_url
from app.models.agent_version import AgentUpdateTask, AgentVersion
from app.models.node import Node
from app.models.user import User

router = APIRouter(prefix="/api/v1/agents", tags=["agent-versions"])
admin_router = APIRouter(prefix="/api/v1/admin/agent-versions", tags=["admin-agent-versions"])
logger = get_logger("api.agent_version")

# 更新包存放目录：可通过环境变量覆盖，默认项目根下的 agent_dist/
# agent_version.py 位于 backend/app/api/，项目根为其上三级
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
AGENT_DIST_DIR = Path(os.getenv("CHAOSOPS_AGENT_DIST_DIR", str(PROJECT_ROOT / "agent_dist")))

# 更新任务 running 状态超时回收阈值（秒）。进程崩溃等导致 running 卡死时，
# 超过该时长仍未上报结果的任务会被回收为 failed，避免永久占用。
RUNNING_TIMEOUT_SECONDS = int(os.getenv("AGENT_UPDATE_RUNNING_TIMEOUT_SECONDS", "1800"))


def _get_dist_dir() -> Path:
    """获取并确保更新包目录存在。"""
    AGENT_DIST_DIR.mkdir(parents=True, exist_ok=True)
    return AGENT_DIST_DIR


class AgentVersionCreate(BaseModel):
    """发布新版本请求体。"""

    version: str = Field(..., min_length=1, description="版本号，如 1.2.5")
    channel: str = Field(default="stable", description="更新通道：stable / beta")
    filename: str = Field(..., min_length=1, description="更新包文件名")
    checksum: str = Field(..., description="SHA256 校验值")
    is_mandatory: bool = Field(default=False, description="是否强制更新")
    release_notes: Optional[str] = Field(default=None, description="版本说明")


class AgentVersionRead(BaseModel):
    """版本记录响应体。"""

    id: int
    version: str
    channel: str
    filename: str
    checksum: str
    is_mandatory: bool
    release_notes: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class AgentLatestVersionResponse(BaseModel):
    """Agent 查询最新版本响应体。"""

    latest: Optional[str] = None
    download_url: Optional[str] = None
    checksum: Optional[str] = None
    mandatory: bool = False
    release_notes: Optional[str] = None


class AgentUpdateTaskRead(BaseModel):
    """Agent 更新任务响应体。

    额外携带目标版本的 ``checksum`` 与 ``download_url``，使 Agent 无需再调用
    admin 接口即可取得校验信息（修复 Agent Token 调 admin 接口被 401 吞掉导致
    checksum 为空、校验必败的问题）。
    """

    task_id: str
    node_id: str
    target_version: str
    status: str
    error_message: Optional[str] = None
    result: Optional[str] = None
    created_at: datetime
    checksum: Optional[str] = None
    download_url: Optional[str] = None

    model_config = {"from_attributes": True}


class AgentUpdateTaskResult(BaseModel):
    """Agent 上报更新任务结果请求体。"""

    success: bool
    error_message: Optional[str] = None
    result: Optional[str] = None


@admin_router.post("", response_model=AgentVersionRead, status_code=status.HTTP_201_CREATED)
def publish_agent_version(
    request: Request,
    body: AgentVersionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> AgentVersion:
    """发布新的 Agent 版本。"""
    # 版本号格式校验：x.y.z
    if not re.match(r"^\d+\.\d+\.\d+$", body.version):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "agent_version.invalid_format"),
        )

    if body.channel not in {"stable", "beta"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "agent_version.invalid_channel"),
        )

    # checksum 应为 64 位十六进制
    if len(body.checksum) != 64 or not re.match(r"^[0-9a-fA-F]{64}$", body.checksum):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "agent_version.invalid_checksum"),
        )

    existing = db.exec(select(AgentVersion).where(AgentVersion.version == body.version)).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail(request, "agent_version.exists", version=body.version),
        )

    version_record = AgentVersion(
        version=body.version,
        channel=body.channel,
        filename=body.filename,
        checksum=body.checksum.lower(),
        is_mandatory=body.is_mandatory,
        release_notes=body.release_notes,
    )
    db.add(version_record)
    db.commit()
    db.refresh(version_record)
    logger.info(f"发布 Agent 版本: {body.version}, 通道: {body.channel}")
    return version_record


@admin_router.get("", response_model=list[AgentVersionRead])
def list_agent_versions(
    channel: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> list[AgentVersion]:
    """列出已发布的 Agent 版本。"""
    statement = select(AgentVersion).order_by(desc(AgentVersion.created_at))
    if channel is not None:
        statement = statement.where(AgentVersion.channel == channel)
    return list(db.exec(statement).all())


@router.get("/version", response_model=AgentLatestVersionResponse)
def get_latest_version(
    request: Request,
    current: Optional[str] = None,
    channel: str = "stable",
    db: Session = Depends(get_db),
    current_node: Node = Depends(get_current_node),
):
    """Agent 查询当前推荐版本。

    当前节点认证通过后返回 latest、download_url、checksum 等信息。
    """
    if channel not in {"stable", "beta"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "agent_version.invalid_channel"),
        )

    latest = db.exec(
        select(AgentVersion)
        .where(AgentVersion.channel == channel)
        .order_by(desc(AgentVersion.created_at))
    ).first()

    if latest is None:
        return AgentLatestVersionResponse()

    # 如果 current 已是最新版，则不返回下载信息
    if current and current == latest.version:
        return AgentLatestVersionResponse(latest=latest.version)

    try:
        public_url = resolve_public_server_url(request)
    except HTTPException:
        public_url = ""

    download_url = f"{public_url}/api/v1/agents/dist/{latest.filename}" if public_url else None

    return AgentLatestVersionResponse(
        latest=latest.version,
        download_url=download_url,
        checksum=f"sha256:{latest.checksum}",
        mandatory=latest.is_mandatory,
        release_notes=latest.release_notes,
    )


@router.get("/dist/latest")
def download_latest_agent_dist(
    install_key: str,
    db: Session = Depends(get_db),
):
    """curl 一键安装场景下载最新 Agent 分发包。

    鉴权方式：校验 install_key 有效性（存在/未使用/未过期/节点未删除），
    但**不消耗**——install_key 是建节点一次性凭证，在 ``POST /register``
    注册时才标记作废，下载与注册共用同一把 key。

    必须定义在 ``/dist/{filename}`` 之前，否则 "latest" 会被当作 filename
    匹配到需要 agent_token 鉴权的更新包下载端点。
    """
    from fastapi.responses import FileResponse

    # 校验逻辑与 agents.py /register 保持一致（统一错误信息，防止枚举 Install Key）
    node = db.exec(select(Node).where(Node.install_key == install_key)).first()
    invalid_key_error = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="安装密钥无效或已过期",
    )
    if node is None or node.is_deleted:
        raise invalid_key_error
    if node.install_key_used:
        raise invalid_key_error
    if datetime.now(timezone.utc) > ensure_utc(node.install_key_expires_at):
        raise invalid_key_error
    # 注意：此处只校验不消耗，不写 install_key_used、不 commit

    dist_dir = _get_dist_dir().resolve()

    # 从分发目录中解析文件名版本号，取最新的 chaosops-agent-<x.y.z>.tar.gz
    version_pattern = re.compile(r"^chaosops-agent-(\d+)\.(\d+)\.(\d+)\.tar\.gz$")
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for entry in dist_dir.iterdir():
        match = version_pattern.match(entry.name)
        if match and entry.is_file():
            candidates.append(
                ((int(match.group(1)), int(match.group(2)), int(match.group(3))), entry.name)
            )

    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="未找到 agent 分发包，请先运行 scripts/build-agent-package.sh",
        )

    candidates.sort(key=lambda item: item[0])
    filename = candidates[-1][1]
    file_path = (dist_dir / filename).resolve()

    # 文件名虽是自己扫描得到，仍做路径穿越防护（纵深防御）
    try:
        file_path.relative_to(dist_dir)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的分发包文件名",
        ) from exc

    # sha256：优先读同名 .sha256 文件首字段（构建脚本产出），否则实时计算
    sha256_file = file_path.with_name(file_path.name + ".sha256")
    checksum: Optional[str] = None
    if sha256_file.exists() and sha256_file.is_file():
        first_field = sha256_file.read_text(encoding="utf-8").split()
        if first_field and re.match(r"^[0-9a-fA-F]{64}$", first_field[0]):
            checksum = first_field[0].lower()
    if checksum is None:
        digest = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        checksum = digest.hexdigest()

    logger.info(f"install_key 校验通过，分发 Agent 包: {filename}")
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/gzip",
        headers={"X-Agent-Checksum": checksum},
    )


@router.get("/dist/{filename}")
def download_agent_dist(
    request: Request,
    filename: str,
    current_node: Node = Depends(get_current_node),
):
    """Agent 下载更新包。"""
    from fastapi.responses import FileResponse

    dist_dir = _get_dist_dir().resolve()
    file_path = (dist_dir / filename).resolve()

    # 防止路径穿越
    try:
        file_path.relative_to(dist_dir)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "agent_version.invalid_filename"),
        ) from exc

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "agent_version.dist_not_found"),
        )

    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/gzip",
    )


def reclaim_stale_running_tasks(
    db: Session, timeout_seconds: Optional[int] = None
) -> int:
    """将超时的 running 更新任务回收为 failed。

    进程崩溃/重启可能导致任务永远卡在 running；本函数把 running 时长超过
    ``timeout_seconds``（默认 RUNNING_TIMEOUT_SECONDS）的任务标记为 failed，释放占用。
    返回被回收的任务数。
    """
    timeout = timeout_seconds or RUNNING_TIMEOUT_SECONDS
    now = now_utc()
    cutoff = now - timedelta(seconds=timeout)
    result = db.execute(
        update(AgentUpdateTask)
        .where(
            AgentUpdateTask.status == "running",
            AgentUpdateTask.updated_at < cutoff,
        )
        .values(
            status="failed",
            error_message="running 超时，被系统自动回收",
            finished_at=now,
            updated_at=now,
        )
    )
    reclaimed = result.rowcount or 0
    if reclaimed:
        db.commit()
        logger.warning(f"回收 {reclaimed} 个超时的 running 更新任务")
    return reclaimed


def _build_update_task_read(
    task: AgentUpdateTask, request: Optional[Request], db: Session
) -> AgentUpdateTaskRead:
    """构造更新任务响应，附带目标版本 checksum 与下载地址。"""
    read = AgentUpdateTaskRead.model_validate(task)
    version = db.exec(
        select(AgentVersion).where(AgentVersion.version == task.target_version)
    ).first()
    if version is not None:
        read.checksum = f"sha256:{version.checksum}"
        public_url = ""
        if request is not None:
            try:
                public_url = resolve_public_server_url(request)
            except HTTPException:
                public_url = ""
        if public_url:
            read.download_url = f"{public_url}/api/v1/agents/dist/{version.filename}"
    return read


@router.get("/update-task", response_model=Optional[AgentUpdateTaskRead])
def get_pending_update_task(
    request: Request,
    db: Session = Depends(get_db),
    current_node: Node = Depends(get_current_node),
) -> Optional[AgentUpdateTaskRead]:
    """Agent 拉取分配给当前节点的待执行更新任务。

    使用原子 ``UPDATE ... WHERE status='pending'`` 认领，保证并发轮询下只有一份
    能成功将任务置为 running；同时回收超时的 running 任务。响应直接附带目标版本
    的 checksum 与下载地址，Agent 无需再访问 admin 接口。
    """
    # 先回收超时的 running 任务（启动后/进程崩溃残留）
    reclaim_stale_running_tasks(db)

    candidate = db.exec(
        select(AgentUpdateTask)
        .where(
            AgentUpdateTask.node_id == current_node.node_id,
            AgentUpdateTask.status == "pending",
        )
        .order_by(AgentUpdateTask.created_at)
    ).first()

    if candidate is None:
        return None

    # 原子认领：仅当状态仍为 pending 时才置为 running，并发下只一份成功
    now = now_utc()
    result = db.execute(
        update(AgentUpdateTask)
        .where(
            AgentUpdateTask.id == candidate.id,
            AgentUpdateTask.status == "pending",
        )
        .values(status="running", updated_at=now)
    )
    if result.rowcount != 1:
        # 被其他并发请求抢先认领
        db.rollback()
        return None
    db.commit()

    task = db.get(AgentUpdateTask, candidate.id)
    logger.info(f"下发更新任务 {task.task_id} 到节点 {current_node.node_id}")
    return _build_update_task_read(task, request, db)


@router.post("/update-task/{task_id}/result", response_model=AgentUpdateTaskRead)
def report_update_task_result(
    request: Request,
    task_id: str,
    body: AgentUpdateTaskResult,
    db: Session = Depends(get_db),
    current_node: Node = Depends(get_current_node),
) -> AgentUpdateTask:
    """Agent 上报更新任务执行结果。"""
    task = db.exec(
        select(AgentUpdateTask).where(AgentUpdateTask.task_id == task_id)
    ).first()

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "agent_version.task_not_found"),
        )

    if task.node_id != current_node.node_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_detail(request, "agent_version.report_forbidden"),
        )

    now = now_utc()
    task.status = "success" if body.success else "failed"
    task.error_message = body.error_message
    task.result = body.result
    task.finished_at = now
    task.updated_at = now
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info(f"更新任务 {task_id} 结果: {task.status}")
    return task


def create_update_task_for_node(
    db: Session,
    node: Node,
    target_version: Optional[str] = None,
) -> AgentUpdateTask:
    """为指定节点创建更新任务。

    如果未指定目标版本，使用当前通道最新版本。
    """
    if target_version is None:
        latest = db.exec(
            select(AgentVersion)
            .where(AgentVersion.channel == "stable")
            .order_by(desc(AgentVersion.created_at))
        ).first()
        if latest is None:
            raise ValueError("没有可用的 Agent 版本")
        target_version = latest.version

    existing_pending = db.exec(
        select(AgentUpdateTask).where(
            AgentUpdateTask.node_id == node.node_id,
            AgentUpdateTask.status.in_(["pending", "running"]),  # type: ignore[attr-defined]
        )
    ).first()
    if existing_pending is not None:
        raise ValueError(f"节点已有进行中的更新任务: {existing_pending.task_id}")

    task_id = f"aut_{secrets.token_urlsafe(16)}"
    for _ in range(3):
        existing = db.exec(select(AgentUpdateTask).where(AgentUpdateTask.task_id == task_id)).first()
        if existing is None:
            break
        task_id = f"aut_{secrets.token_urlsafe(16)}"
    else:
        raise RuntimeError("无法生成唯一的更新任务 ID")

    task = AgentUpdateTask(
        task_id=task_id,
        node_id=node.node_id,
        target_version=target_version,
        status="pending",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info(f"为节点 {node.node_id} 创建更新任务 {task.task_id} -> {target_version}")
    return task
