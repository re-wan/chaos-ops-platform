"""通用加密工具。

使用 Fernet 对称加密，密钥从 settings.SECRET_KEY 派生。
主要用于加密 API Key、Token 等敏感字符串。

注意：本模块两类派生方式并存——
- ``encrypt_value``/``decrypt_value`` 等历史函数沿用 sha256 直接派生，
  存量密文（LLM API Key 等）依赖其稳定性，不得改动；
- ``encrypt_webhook_secret``/``decrypt_webhook_secret``（收尾修复第 5 批新增）
  采用 PBKDF2/sha256 派生 32 字节密钥并带用途 salt 隔离，强度更高，
  仅用于开放 API Webhook 订阅 secret。
"""

import base64
import hashlib
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.core.config import settings


def _get_fernet() -> Fernet:
    """从 SECRET_KEY 派生 Fernet 密钥。"""
    key = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_value(plaintext: str) -> str:
    """加密字符串，返回 Base64 编码的密文。"""
    fernet = _get_fernet()
    return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_value(ciphertext: str) -> str:
    """解密密文字符串。"""
    fernet = _get_fernet()
    return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def encrypt_dict(config: dict[str, Any]) -> str:
    """加密配置字典，返回 Base64 编码的密文字符串。"""
    import json

    fernet = _get_fernet()
    plaintext = json.dumps(config, ensure_ascii=False)
    return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_dict(ciphertext: str) -> dict[str, Any]:
    """解密密文字符串，返回配置字典。"""
    import json

    fernet = _get_fernet()
    plaintext = fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    return json.loads(plaintext)


# ---------------------------------------------------------------------------
# Webhook 订阅 secret 加密（收尾修复第 5 批）
# ---------------------------------------------------------------------------

# 用途隔离 salt：与历史 sha256 派生路径区分开，即使 SECRET_KEY 相同，
# 两类密文也互不通用。
_WEBHOOK_SECRET_KDF_SALT = b"chaosops:webhook-secret:v1"
# PBKDF2/sha256 迭代次数（OWASP 2023 推荐量级）；仅派生一次并缓存，
# 不影响每次投递的开销。
_WEBHOOK_SECRET_KDF_ITERATIONS = 480_000


class WebhookSecretError(Exception):
    """Webhook secret 解密失败（密文损坏，或 SECRET_KEY 与加密时不一致）。"""


@lru_cache(maxsize=8)
def _webhook_fernet_for(secret_key: str) -> Fernet:
    """PBKDF2/sha256 从 SECRET_KEY 派生 32 字节密钥并构造 Fernet（按密钥值缓存）。

    派生是一次性 CPU 开销；缓存避免每次投递重复 KDF。SECRET_KEY 轮换后新值
    会派生出新密钥，历史密文解密失败 → 调用方 fail-closed。
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_WEBHOOK_SECRET_KDF_SALT,
        iterations=_WEBHOOK_SECRET_KDF_ITERATIONS,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret_key.encode("utf-8")))
    return Fernet(key)


def encrypt_webhook_secret(plaintext: str) -> str:
    """加密 Webhook 订阅 secret，返回可入库的密文字符串。"""
    fernet = _webhook_fernet_for(settings.SECRET_KEY)
    return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_webhook_secret(ciphertext: str) -> str:
    """解密 Webhook 订阅 secret。

    Raises:
        WebhookSecretError: 密文无效或当前 SECRET_KEY 与加密时不同（轮换）。
            调用方必须 fail-closed（拒绝投递），严禁回退到用密文签名。
    """
    fernet = _webhook_fernet_for(settings.SECRET_KEY)
    try:
        return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise WebhookSecretError(
            "Webhook secret 解密失败：密文无效或 SECRET_KEY 已轮换"
        ) from exc
