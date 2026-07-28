"""节点管理相关 Pydantic Schema。"""

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# 节点名称：字母/数字/中划线/下划线/点
NODE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
# 分组名：字母/数字/中划线/下划线/点
GROUP_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
# 标签 key/value：字母/数字/中划线/下划线/点/斜杠/冒号/@
LABEL_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\./@]+$")
LABEL_VALUE_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\./:@]+$")

MAX_BULK_NODES = 100
LABEL_MAX_LENGTH = 128


def _validate_single_label(key: str, value: str) -> None:
    """校验单个标签键值对是否合法。"""
    if len(key) > LABEL_MAX_LENGTH:
        raise ValueError(f"标签 key '{key}' 超过最大长度 {LABEL_MAX_LENGTH}")
    if len(value) > LABEL_MAX_LENGTH:
        raise ValueError(f"标签 value '{value}' 超过最大长度 {LABEL_MAX_LENGTH}")
    if not LABEL_KEY_PATTERN.match(key):
        raise ValueError(f"标签 key '{key}' 包含非法字符")
    if not LABEL_VALUE_PATTERN.match(value):
        raise ValueError(f"标签 value '{value}' 包含非法字符")


class NodeCreate(BaseModel):
    """创建节点请求体。"""

    name: str = Field(..., min_length=1, max_length=64, description="节点名称，全局唯一")
    host: Optional[str] = Field(default=None, description="主机地址或域名")
    description: Optional[str] = Field(default=None, description="节点描述")
    platform: str = Field(default="linux", description="平台：linux / windows")
    group: Optional[str] = Field(default=None, max_length=64, description="节点分组")
    labels: Optional[dict] = Field(default=None, description="标签键值对")

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v: str) -> str:
        if v not in {"linux", "windows"}:
            raise ValueError("平台必须是 linux 或 windows")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not NODE_NAME_PATTERN.match(v):
            raise ValueError("节点名称只能包含字母、数字、中划线、下划线和点")
        return v

    @field_validator("group")
    @classmethod
    def validate_group(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not GROUP_PATTERN.match(v):
            raise ValueError("分组名只能包含字母、数字、中划线、下划线和点")
        return v

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, v: Optional[dict]) -> Optional[dict]:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("labels 必须是字典")
        for key, value in v.items():
            if not isinstance(key, str):
                raise ValueError("标签 key 必须是字符串")
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError("标签 value 必须是标量类型")
            _validate_single_label(key, str(value))
        return v


class NodeUpdate(BaseModel):
    """更新节点请求体。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    host: Optional[str] = None
    description: Optional[str] = None
    platform: Optional[str] = None
    group: Optional[str] = Field(default=None, max_length=64)
    labels: Optional[dict] = None

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in {"linux", "windows"}:
            raise ValueError("平台必须是 linux 或 windows")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not NODE_NAME_PATTERN.match(v):
            raise ValueError("节点名称只能包含字母、数字、中划线、下划线和点")
        return v

    @field_validator("group")
    @classmethod
    def validate_group(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not GROUP_PATTERN.match(v):
            raise ValueError("分组名只能包含字母、数字、中划线、下划线和点")
        return v

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, v: Optional[dict]) -> Optional[dict]:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("labels 必须是字典")
        for key, value in v.items():
            if not isinstance(key, str):
                raise ValueError("标签 key 必须是字符串")
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError("标签 value 必须是标量类型")
            _validate_single_label(key, str(value))
        return v


class NodeRead(BaseModel):
    """节点响应模型（不含认证敏感字段）。"""

    id: int
    node_id: str
    name: str
    host: Optional[str]
    description: Optional[str]
    platform: str
    group: Optional[str]
    labels: Optional[dict]
    status: str
    last_seen: Optional[datetime]
    is_local: bool
    is_deleted: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NodeInstallInfo(BaseModel):
    """安装命令响应模型。"""

    linux_command: str
    windows_command: str
    install_key_expires_at: str


class BulkNodeItem(BaseModel):
    """单条批量创建项。

    字段保持宽松，由业务层逐条校验并返回失败隔离结果，
    避免单条数据错误导致整个请求被 Pydantic 拒绝。
    """

    name: Optional[str] = Field(default=None, max_length=64, description="节点名称")
    host: Optional[str] = Field(default=None, max_length=255, description="主机地址或域名")
    platform: Optional[str] = Field(default=None, description="平台：linux / windows")
    group: Optional[str] = Field(default=None, max_length=64, description="节点分组")
    labels: Optional[dict] = Field(default=None, description="标签键值对")
    description: Optional[str] = Field(default=None, description="节点描述")


class BulkNodeCreateRequest(BaseModel):
    """JSON 批量创建请求体。"""

    nodes: list[BulkNodeItem] = Field(..., description="待批量创建的节点列表")


class BulkNodeResultItem(BaseModel):
    """单条批量创建结果。"""

    name: Optional[str] = None
    node_id: Optional[str] = None
    status: str = Field(..., description="success 或 failure")
    error: Optional[str] = None


class BulkNodeCreateResponse(BaseModel):
    """批量创建接口响应。"""

    task_id: int
    total_count: int
    success_count: int
    failure_count: int
    results: list[BulkNodeResultItem]
    install_script_url: str
