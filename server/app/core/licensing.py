"""本地 License 校验与功能开关服务。

License 文件格式为 JSON，带 RSA-SHA256 签名。Server 启动时加载并缓存到内存，
上传新 License 后刷新缓存。所有功能判断统一通过本模块查询。

安全约束（Phase 2 隐患清除）：
- 本模块**只负责校验**，不再内置任何可签名的私钥，也不再提供签名函数。
  签发逻辑只保留在 ``scripts/generate_license.py``。
- 公钥**只**从外部文件加载（``deploy/license_keys/public.pem`` 或
  ``LICENSE_PUBLIC_KEY_PATH`` 指定的路径）。读不到或解析失败时**直接拒绝**
  （视为无 License/免费版），**禁止回退到任何内嵌公钥**（fail-closed）。
"""

import base64
import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.exceptions import InvalidSignature

from app.core.build_edition import BUILD_EDITION
from app.core.config import settings
from app.core.logger import get_logger
from app.core.utils import ensure_utc
from app.models.license import LicenseInfo

logger = get_logger("licensing")

# 版本秩：用于构建版本天花板比较（free < professional < enterprise）。
_EDITION_RANK = {"free": 0, "professional": 1, "enterprise": 2}


def _apply_build_ceiling(edition: str) -> str:
    """构建版本天花板：有效版本不超过构建版本（三版物理分包）。"""
    ceiling = _EDITION_RANK.get(BUILD_EDITION, len(_EDITION_RANK) - 1)
    if _EDITION_RANK.get(edition, 0) > ceiling:
        return BUILD_EDITION
    return edition


# 版本功能矩阵。
# 数值 -1 表示无限制；布尔值表示是否解锁；字符串 "trial" 表示试用可用。
# 注：自 Phase 3 门控补全起矩阵内不再使用 "trial"（professional 的 ai_heal 已转正），
# 但 check_feature_enabled/is_feature_trial 仍保留对 "trial" 的处理以兼容旧数据。
_FEATURE_MATRIX = {
    "free": {
        "basic_monitoring": True,
        "alert_rules": 5,
        "max_nodes": 3,
        "metric_retention_days": 7,
        "influxdb": False,
        "ai_heal": False,
        "remote_execution": False,
        "bulk_registration": False,
        "im_channels": False,
        "multi_user_rbac": False,
        "ai_custom_tool": False,
        "ai_self_optimization": False,
        "open_api": False,
    },
    "professional": {
        "basic_monitoring": True,
        "alert_rules": 50,
        "max_nodes": 20,
        "metric_retention_days": 30,
        "influxdb": True,
        "ai_heal": True,
        "remote_execution": True,
        "bulk_registration": True,
        "im_channels": True,
        "multi_user_rbac": True,
        "ai_custom_tool": False,
        "ai_self_optimization": False,
        "open_api": False,
    },
    "enterprise": {
        "basic_monitoring": True,
        "alert_rules": -1,
        "max_nodes": -1,
        "metric_retention_days": 90,
        "influxdb": True,
        "ai_heal": True,
        "remote_execution": True,
        "bulk_registration": True,
        "im_channels": True,
        "multi_user_rbac": True,
        "ai_custom_tool": True,
        "ai_self_optimization": True,
        "open_api": True,
    },
}

_cached_license: Optional[LicenseInfo] = None


def _get_public_key_path() -> Optional[str]:
    """返回外部公钥文件路径，找不到则返回 None（fail-closed）。

    解析顺序：
    1. 环境变量 ``LICENSE_PUBLIC_KEY_PATH`` 指定的路径（存在时优先，便于自定义部署与测试注入）。
    2. V2 目录结构 ``config/license_keys/public.pem``。
    3. 向后兼容 ``deploy/license_keys/public.pem``（V1 目录结构）。
    4. 都不存在则返回 ``None``，由调用方按"无公钥"拒绝处理。
    """
    env_path = os.environ.get("LICENSE_PUBLIC_KEY_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    project_root = Path(__file__).resolve().parent.parent.parent.parent

    # V2 目录结构：安装器将公钥部署到 config/license_keys/
    v2_path = project_root / "config" / "license_keys" / "public.pem"
    if v2_path.exists():
        return str(v2_path)

    # V1 兼容：项目根目录 deploy/license_keys/
    v1_path = project_root / "deploy" / "license_keys" / "public.pem"
    if v1_path.exists():
        return str(v1_path)
    return None


def _load_public_key():
    """加载 RSA 公钥；失败时返回 None（fail-closed）。

    安全要求：只从外部文件加载，**绝不回退到内嵌公钥**。读不到或解析失败一律
    返回 None，使后续 License 校验按"无公钥"拒绝并降级为免费版。
    """
    public_key_path = _get_public_key_path()
    if not public_key_path:
        return None
    try:
        with open(public_key_path, "rb") as f:
            return serialization.load_pem_public_key(f.read())
    except Exception as exc:  # noqa: BLE001
        # 公钥损坏/格式错误也按 fail-closed 处理：拒绝校验，而非放行
        logger.warning(f"读取 License 公钥 {public_key_path} 失败，将拒绝校验: {exc}")
        return None


def get_server_install_id() -> str:
    """基于机器指纹生成 Server 安装 ID。

    使用 hostname + MAC 地址 + ``SERVER_INSTALL_ID_SALT`` 的 SHA-256 前 16 字节，
    并以 ``si_`` 为前缀。MAC 获取失败时使用 "unknown" 占位。

    注意：salt 必须由部署侧随机生成（见 install-server.sh），不再是固定串，
    否则任何拿到源码的人都能离线定向伪造绑定本机的 License。
    """
    try:
        hostname = socket.gethostname()
    except Exception:  # noqa: BLE001
        hostname = "unknown-host"

    try:
        mac = _get_mac()
    except Exception:  # noqa: BLE001
        mac = "unknown-mac"

    raw = f"{hostname}:{mac}:{settings.SERVER_INSTALL_ID_SALT}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"si_{digest}"


def _get_mac() -> str:
    """获取本机 MAC 地址字符串。"""
    import uuid

    return hex(uuid.getnode())


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """解析 ISO 8601 时间字符串为 UTC datetime。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return ensure_utc(dt)
    except ValueError:
        return None


def _build_payload(license_dict: dict) -> str:
    """构建用于签名的规范化 payload（除 signature 外按 key 排序的 JSON）。"""
    payload = {k: v for k, v in license_dict.items() if k != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _verify_signature_with_key(public_key, payload: dict, signature: str) -> bool:
    """使用给定公钥验证 RSA-SHA256 签名。"""
    try:
        message = _build_payload(payload).encode("utf-8")
        sig_bytes = base64.b64decode(signature)
        public_key.verify(
            sig_bytes,
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"License 签名验证异常: {exc}")
        return False


def verify_license_signature(payload: dict, signature: str) -> bool:
    """验证 RSA-SHA256 签名；无可用公钥时按 fail-closed 返回 False。

    Args:
        payload: 除 signature 外的 License 字段字典。
        signature: Base64 编码的签名。

    Returns:
        签名有效返回 True，否则（含无公钥/公钥损坏）返回 False。
    """
    public_key = _load_public_key()
    if public_key is None:
        return False
    return _verify_signature_with_key(public_key, payload, signature)


def _make_free_license(
    error_message: Optional[str] = None, is_valid: bool = True
) -> LicenseInfo:
    """构造默认免费版 License。

    Args:
        error_message: 校验失败时的错误信息；无错误时为空。
        is_valid: 是否视为有效。文件不存在时为 True；校验失败降级时为 False。
    """
    return LicenseInfo(
        license_id="free",
        edition="free",
        max_nodes=3,
        issued_at=None,
        expires_at=None,
        server_install_id=None,
        signature=None,
        is_valid=is_valid,
        error_message=error_message,
    )


def _validate_license(license_dict: dict) -> LicenseInfo:
    """校验 License 字典并返回 LicenseInfo。

    校验失败时返回降级为 free 功能的 LicenseInfo，并附带错误信息。
    """
    required_fields = [
        "license_id",
        "edition",
        "max_nodes",
        "issued_at",
        "expires_at",
        "server_install_id",
        "signature",
    ]
    for field in required_fields:
        if field not in license_dict:
            return _make_free_license(
                error_message=f"License 文件缺少必要字段: {field}", is_valid=False
            )

    edition = license_dict.get("edition")
    if edition not in _FEATURE_MATRIX:
        return _make_free_license(
            error_message=f"无效的 License 版本: {edition}", is_valid=False
        )

    # fail-closed：无可用公钥时直接拒绝，绝不回退内嵌公钥
    public_key = _load_public_key()
    if public_key is None:
        return _make_free_license(
            error_message="License 公钥不可用，无法校验签名（已降级为免费版）",
            is_valid=False,
        )

    signature = license_dict.get("signature")
    if not _verify_signature_with_key(public_key, license_dict, signature):
        return _make_free_license(error_message="License 签名无效", is_valid=False)

    server_install_id = license_dict.get("server_install_id")
    if server_install_id != get_server_install_id():
        return _make_free_license(
            error_message="License 绑定的安装 ID 与当前服务器不匹配",
            is_valid=False,
        )

    expires_at = _parse_iso_datetime(license_dict.get("expires_at"))
    issued_at = _parse_iso_datetime(license_dict.get("issued_at"))
    now = now_utc()

    if issued_at is not None and now < issued_at:
        return _make_free_license(error_message="License 尚未生效", is_valid=False)

    if expires_at is not None and now > expires_at:
        # 过期：保留原 edition 用于显示，但标记为无效；功能上降级为 free。
        return LicenseInfo(
            license_id=license_dict.get("license_id", ""),
            edition=edition,
            max_nodes=license_dict.get("max_nodes", 3),
            issued_at=issued_at,
            expires_at=expires_at,
            server_install_id=server_install_id,
            signature=signature,
            is_valid=False,
            error_message="License 已过期",
        )

    return LicenseInfo(
        license_id=license_dict.get("license_id", ""),
        edition=edition,
        max_nodes=license_dict.get("max_nodes", 3),
        issued_at=issued_at,
        expires_at=expires_at,
        server_install_id=server_install_id,
        signature=signature,
        is_valid=True,
        error_message=None,
    )


def _effective_edition(license_info: Optional[LicenseInfo]) -> str:
    """返回用于功能判断的有效 edition。

    License 无效或过期时降级为 free。
    """
    if license_info is None:
        return "free"
    if not license_info.is_valid:
        return "free"
    edition = license_info.edition
    if edition not in _FEATURE_MATRIX:
        return "free"
    return _apply_build_ceiling(edition)


def get_current_edition() -> str:
    """返回当前用于功能判断的有效 edition（无效/过期降级为 free）。

    供开放 API 等需要按 edition 分层（如限流）的场景使用；
    与 ``get_license_features`` 使用同一降级规则，避免把已降级 License
    误判为高配版本。
    """
    return _effective_edition(get_cached_license())


def load_license(path: Optional[str] = None) -> LicenseInfo:
    """加载本地 License 文件并校验。

    Args:
        path: License 文件路径，默认使用 settings.LICENSE_FILE_PATH。

    Returns:
        校验后的 LicenseInfo；无文件或校验失败时返回默认 free License。
    """
    path = path or settings.LICENSE_FILE_PATH

    if not os.path.exists(path):
        logger.info(f"未找到 License 文件 {path}，使用默认免费版")
        return _make_free_license()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.warning(f"License 文件 JSON 解析失败: {e}")
        return _make_free_license(
            error_message=f"License 文件格式错误: {e}", is_valid=False
        )
    except OSError as e:
        logger.warning(f"读取 License 文件失败: {e}")
        return _make_free_license(
            error_message=f"读取 License 文件失败: {e}", is_valid=False
        )

    info = _validate_license(data)
    if info.is_valid:
        logger.info(
            f"License 加载成功: edition={info.edition}, "
            f"max_nodes={info.max_nodes}, expires_at={info.expires_at}"
        )
    else:
        logger.warning(f"License 校验失败: {info.error_message}，降级为免费版")
    return info


def refresh_license() -> LicenseInfo:
    """重新加载 License 文件并刷新全局缓存。"""
    global _cached_license
    _cached_license = load_license()
    return _cached_license


def get_cached_license() -> LicenseInfo:
    """获取当前缓存的 License 信息。"""
    global _cached_license
    if _cached_license is None:
        _cached_license = load_license()
    return _cached_license


def get_license_features(license_info: Optional[LicenseInfo] = None) -> dict:
    """返回当前有效版本的功能矩阵。"""
    if license_info is None:
        license_info = get_cached_license()
    edition = _effective_edition(license_info)
    features = dict(_FEATURE_MATRIX[edition])
    # License 有效时，优先使用 License 中指定的 max_nodes（-1 为无限）
    if (
        license_info is not None
        and license_info.is_valid
        and license_info.max_nodes is not None
    ):
        features["max_nodes"] = license_info.max_nodes
    return features


def check_feature_enabled(feature: str, license_info: Optional[LicenseInfo] = None) -> bool:
    """检查指定功能是否已解锁。

    对试用功能（"trial"）也返回 True，调用方可通过 is_feature_trial 进一步判断。
    """
    features = get_license_features(license_info)
    value = features.get(feature, False)
    if value is True:
        return True
    if value == "trial":
        return True
    return False


def is_feature_trial(feature: str, license_info: Optional[LicenseInfo] = None) -> bool:
    """检查指定功能是否处于试用状态。"""
    features = get_license_features(license_info)
    return features.get(feature) == "trial"


def get_max_nodes() -> int:
    """返回当前 License 允许的最大节点数，-1 表示无限制。"""
    features = get_license_features()
    return features.get("max_nodes", 3)


def get_max_alert_rules() -> int:
    """返回当前 License 允许的告警规则数量上限，-1 表示无限制。"""
    features = get_license_features()
    return features.get("alert_rules", 5)


def check_max_nodes_allowed(current_count: int) -> bool:
    """检查当前节点数是否未超过 License 上限。

    Args:
        current_count: 当前已有节点数量（不含软删除）。

    Returns:
        允许新增返回 True，否则返回 False。
    """
    max_nodes = get_max_nodes()
    if max_nodes < 0:
        return True
    return current_count < max_nodes


def is_license_valid() -> bool:
    """返回当前 License 是否通过校验。"""
    return get_cached_license().is_valid


def now_utc() -> datetime:
    """返回当前 UTC 时间。"""
    return datetime.now(timezone.utc)
