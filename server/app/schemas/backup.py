"""备份管理 API 的 Schema。"""

from datetime import datetime

from pydantic import BaseModel


class BackupRead(BaseModel):
    """备份文件信息。

    restorable: 是否可通过 API 恢复。PostgreSQL ``.dump`` 备份为 False（仅展示）。
    """

    filename: str
    size_bytes: int
    created_at: datetime
    verified: bool
    restorable: bool = True


class BackupResponse(BaseModel):
    """统一备份操作响应。"""

    message: str
    backup: BackupRead | None = None
