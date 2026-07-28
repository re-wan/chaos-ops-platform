"""Agent 认证、注册、安装脚本与本地 Agent 动作相关 API 路由。"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.api.deps import get_current_node, get_db
from app.core.utils import ensure_utc
from app.core.config import settings
from app.core.utils import resolve_public_server_url
from app.core.logger import get_logger
from app.models.node import Node
from app.services.node_service import touch_node_online

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])
logger = get_logger("api.agents")

# 本地 Agent 专属动作
LOCAL_ONLY_ACTIONS = {"restart_server", "cleanup_logs", "vacuum_database"}


class AgentRegisterRequest(BaseModel):
    """Agent 注册请求体。"""

    install_key: str = Field(..., min_length=1, description="一次性安装密钥")
    hostname: str = Field(default="", description="Agent 主机名")
    os: str = Field(default="", description="操作系统")
    arch: str = Field(default="", description="系统架构")
    version: str = Field(default="", description="Agent 版本")


class AgentRegisterResponse(BaseModel):
    """Agent 注册响应体。"""

    agent_token: str
    node_id: str
    heartbeat_interval: int


@router.post("/register", response_model=AgentRegisterResponse)
def register_agent(body: AgentRegisterRequest, db: Session = Depends(get_db)) -> dict:
    """Agent 使用 Install Key 注册，换取长期 Agent Token。

    注册成功后，Install Key 立即失效并记录使用时间。
    """
    node = db.exec(select(Node).where(Node.install_key == body.install_key)).first()

    # 统一错误信息，防止枚举 Install Key
    invalid_key_error = HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="安装密钥无效或已过期",
    )

    if node is None or node.is_deleted:
        raise invalid_key_error

    if node.install_key_used:
        raise invalid_key_error

    now = datetime.now(timezone.utc)
    if now > ensure_utc(node.install_key_expires_at):
        raise invalid_key_error

    # 原子性标记 Install Key 已使用
    node.install_key_used = True
    node.install_key_used_at = now
    db.add(node)
    db.commit()
    db.refresh(node)

    # 更新节点为在线状态并发布事件
    touch_node_online(db, node)

    return {
        "agent_token": node.agent_token,
        "node_id": node.node_id,
        "heartbeat_interval": settings.AGENT_HEARTBEAT_INTERVAL,
    }


def _get_public_server_url(request: Request) -> str:
    """获取对外的 Server URL（统一入口）。

    实际解析逻辑已收敛到 ``app.core.utils.resolve_public_server_url``，
    本函数保留作为 agents 模块内的便捷别名。
    """
    return resolve_public_server_url(request)


@router.get("/uninstall.sh")
def uninstall_script_sh(request: Request) -> Response:
    """下载 Linux 卸载脚本。"""
    script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "agent" / "uninstall.sh"
    if not script_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="卸载脚本不存在")

    content = script_path.read_text(encoding="utf-8")
    return Response(
        content=content,
        media_type="text/x-shellscript",
        headers={"Content-Disposition": "attachment; filename=uninstall.sh"},
    )


@router.get("/uninstall.bat")
def uninstall_script_bat(request: Request) -> Response:
    """下载 Windows 卸载脚本。"""
    script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "agent" / "uninstall.bat"
    if not script_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="卸载脚本不存在")

    # Windows .bat must keep CRLF line endings; read_bytes avoids newline normalization.
    content = script_path.read_bytes()
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=uninstall.bat"},
    )


@router.get("/install.sh")
def install_script_sh(request: Request) -> Response:
    """下载 Linux 一键安装脚本。"""
    script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "agent" / "install.sh"
    if not script_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="安装脚本不存在")

    content = script_path.read_text(encoding="utf-8")
    return Response(
        content=content,
        media_type="text/x-shellscript",
        headers={"Content-Disposition": "attachment; filename=install.sh"},
    )


@router.get("/install.bat")
def install_script_bat(request: Request) -> Response:
    """下载 Windows 一键安装脚本。"""
    script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "agent" / "install.bat"
    if not script_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="安装脚本不存在")

    # Windows .bat must keep CRLF line endings; read_text strips \r in universal-newlines mode.
    content = script_path.read_bytes()
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=install.bat"},
    )


# 测试用途：一个需要 Agent Token 的受保护接口
@router.get("/protected-test")
def protected_test(current_node: Node = Depends(get_current_node)) -> dict:
    """需要 Agent Token 才能访问的测试接口。"""
    return {"node_id": current_node.node_id, "message": "Agent 认证通过"}


class AgentActionRequest(BaseModel):
    """Agent 执行动作请求体。"""

    action: str = Field(..., min_length=1, description="动作名称")
    params: Optional[dict] = Field(default=None, description="动作参数")


class AgentActionResponse(BaseModel):
    """Agent 执行动作响应体。"""

    action: str
    status: str
    message: str


def _is_localhost(request: Request) -> bool:
    """判断请求是否来自本机。

    生产环境通过 client_host 判断；x-test-local-agent: 1 header 仅在 DEBUG 模式下生效，
    生产（DEBUG=False）一律忽略，防止伪造头绕过本地专属动作的本机校验。
    """
    client_host = request.client.host if request.client else None
    if client_host in {"127.0.0.1", "localhost", "::1", "testserver"}:
        return True
    if settings.DEBUG and request.headers.get("x-test-local-agent") == "1":
        return True
    return False


@router.post("/actions/execute", response_model=AgentActionResponse)
def execute_action(
    body: AgentActionRequest,
    request: Request,
    current_node: Node = Depends(get_current_node),
) -> dict:
    """Agent 执行动作接口。

    本地 Agent 专属动作（restart_server、cleanup_logs、vacuum_database）
    只能由 is_local=true 的节点且来自本机的请求触发。
    """
    action = body.action

    if action in LOCAL_ONLY_ACTIONS:
        if not current_node.is_local:
            logger.warning(f"远程节点 {current_node.node_id} 尝试执行本地专属动作 {action}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="该动作仅限本地 Agent 执行",
            )
        if not _is_localhost(request):
            logger.warning(f"非本机请求尝试执行本地专属动作 {action}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="该动作仅限本机访问",
            )

    # MVP 阶段模拟实现本地动作
    if action == "restart_server":
        logger.warning("本地 Agent 触发 restart_server 动作")
        # 实际生产环境通过 systemd 重启；MVP 测试阶段仅记录日志
        return {"action": action, "status": "triggered", "message": "Server 重启已触发"}

    if action == "cleanup_logs":
        logger.info("本地 Agent 触发 cleanup_logs 动作")
        return {"action": action, "status": "ok", "message": "日志清理完成"}

    if action == "vacuum_database":
        logger.info("本地 Agent 触发 vacuum_database 动作")
        try:
            from app.core.resilience import get_database_file_path

            db_path = get_database_file_path()
            if db_path is not None and db_path.exists():
                import sqlite3

                conn = sqlite3.connect(str(db_path))
                conn.execute("VACUUM")
                conn.close()
                return {"action": action, "status": "ok", "message": "数据库 VACUUM 完成"}
            return {"action": action, "status": "skipped", "message": "非 SQLite 数据库，跳过 VACUUM"}
        except Exception as e:
            logger.error(f"VACUUM 失败: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"数据库 VACUUM 失败: {e}",
            )

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="未知的动作")
