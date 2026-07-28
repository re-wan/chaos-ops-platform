"""Agent 版本与更新任务模型。"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from app.core.utils import now_utc


class AgentVersion(SQLModel, table=True):
    """Agent 版本发布记录表。

    Server 通过本表管理可分发的新版本 Agent 更新包。
    """

    __tablename__ = "agent_versions"

    id: Optional[int] = Field(default=None, primary_key=True)
    version: str = Field(index=True, unique=True, description="Agent 版本号，如 1.2.5")
    channel: str = Field(default="stable", index=True, description="更新通道：stable / beta")
    filename: str = Field(description="更新包文件名，如 chaosops-agent-1.2.5.tar.gz")
    checksum: str = Field(description="更新包 SHA256 校验值")
    is_mandatory: bool = Field(default=False, description="是否强制更新")
    release_notes: Optional[str] = Field(default=None, description="版本说明")
    created_at: datetime = Field(default_factory=now_utc, description="发布时间")


class AgentUpdateTask(SQLModel, table=True):
    """Agent 更新任务表。

    记录管理员手动触发或自动生成的节点更新任务状态。
    """

    __tablename__ = "agent_update_tasks"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True, description="任务业务 ID，前缀 aut_")
    node_id: str = Field(index=True, description="目标节点业务 ID")
    target_version: str = Field(description="目标版本号")
    status: str = Field(default="pending", description="任务状态：pending/running/success/failed/rolled_back")
    error_message: Optional[str] = Field(default=None, description="失败原因")
    result: Optional[str] = Field(default=None, description="执行结果摘要")
    created_at: datetime = Field(default_factory=now_utc, description="创建时间")
    updated_at: datetime = Field(default_factory=now_utc, description="更新时间")
    finished_at: Optional[datetime] = Field(default=None, description="完成时间")
