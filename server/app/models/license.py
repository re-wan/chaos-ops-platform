"""License 信息内存模型。

License 信息不需要持久化到数据库，而是从本地 license.json 文件加载后
缓存到内存。本模块只定义运行时使用的数据结构。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class LicenseInfo:
    """本地 License 文件的运行时内存表示。

    字段说明：
        license_id: 授权 ID。
        edition: 版本类型，如 free / professional / enterprise。
        max_nodes: 允许的最大节点数，-1 表示无限制。
        issued_at: 签发时间（UTC）。
        expires_at: 过期时间（UTC）。
        server_install_id: 绑定的 Server 安装 ID。
        signature: RSA-SHA256 签名（Base64）。
        is_valid: 当前 License 是否通过校验。
        error_message: 校验失败时的错误信息；有效时为空。
    """

    license_id: str = ""
    edition: str = "free"
    max_nodes: int = 3
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    server_install_id: Optional[str] = None
    signature: Optional[str] = None
    is_valid: bool = False
    error_message: Optional[str] = None

    def to_dict(self, include_signature: bool = False) -> dict:
        """转换为字典，默认脱敏（不返回 signature）。"""
        result = {
            "license_id": self.license_id,
            "edition": self.edition,
            "max_nodes": self.max_nodes,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "server_install_id": self.server_install_id,
            "is_valid": self.is_valid,
            "error_message": self.error_message,
        }
        if include_signature:
            result["signature"] = self.signature
        return result
