"""License 签发服务。

从 ``scripts/generate_license.py`` 提取的服务端签发逻辑，供自助生成
（license claim）端点调用。安全约束（规格 §4.3）：

- 签名私钥仅存在于服务端 ``deploy/license_keys/private.pem``（600 权限）；
- 本模块只输出**签名后的 License 文件内容**，私钥对象不离开进程内存，
  绝不出现在任何 API 响应/前端代码中；
- 私钥文件缺失时 fail-closed（抛 LicenseIssuerError），不自动生成生产密钥。
"""

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("services.license_issuer")

# 项目根目录（backend/app/services/license_issuer.py 上溯 3 级）
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

# 版本默认节点数（与 scripts/generate_license.py 保持一致）
_EDITION_DEFAULT_NODES = {
    "free": 3,
    "professional": 20,
    "enterprise": -1,
}


class LicenseIssuerError(Exception):
    """License 签发失败（如私钥缺失/损坏）。内部错误，不暴露细节给前端。"""


def get_license_keys_dir() -> Path:
    """返回 License 密钥对目录（可用 LICENSE_KEYS_DIR 覆盖，测试指向临时目录）。"""
    if settings.LICENSE_KEYS_DIR:
        return Path(settings.LICENSE_KEYS_DIR)
    return _PROJECT_ROOT / "deploy" / "license_keys"


def _load_private_key(keys_dir: Path) -> rsa.RSAPrivateKey:
    """加载签名私钥。失败时 fail-closed，抛 LicenseIssuerError。"""
    private_path = keys_dir / "private.pem"
    try:
        return serialization.load_pem_private_key(
            private_path.read_bytes(), password=None
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"License 签名私钥加载失败: {exc}")
        raise LicenseIssuerError("License 签名私钥不可用") from exc


def build_payload(
    license_id: str,
    edition: str,
    max_nodes: int,
    server_install_id: str,
    expires_at: str | None,
) -> dict:
    """构造 License payload（未签名），字段与 scripts/generate_license.py 一致。"""
    now = datetime.now(timezone.utc)
    return {
        "license_id": license_id,
        "edition": edition,
        "max_nodes": max_nodes,
        "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expires_at,
        "server_install_id": server_install_id,
    }


def sign_payload(payload: dict, private_key: rsa.RSAPrivateKey) -> str:
    """使用 RSA-SHA256 对 License payload 签名（与 generate_license.py 同算法）。"""
    message = json.dumps(
        {k: v for k, v in payload.items() if k != "signature"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def issue_license(license_id: str, install_id: str, edition: str = "professional") -> dict:
    """签发绑定 install_id 的 License（默认专业版、终身授权）。

    Args:
        license_id: 调用方生成的全局唯一 License ID。
        install_id: 客户服务器安装 ID（写入 payload 的 server_install_id 字段）。
        edition: 版本，自助生成仅支持 professional（规格 §1）。

    Returns:
        含 signature 的完整 License 字典（可直接 JSON 序列化为文件下发）。
    """
    private_key = _load_private_key(get_license_keys_dir())
    payload = build_payload(
        license_id=license_id,
        edition=edition,
        max_nodes=_EDITION_DEFAULT_NODES.get(edition, 20),
        server_install_id=install_id,
        expires_at=None,  # 默认终身授权（规格复用 generate_license.py 语义）
    )
    payload["signature"] = sign_payload(payload, private_key)
    return payload
