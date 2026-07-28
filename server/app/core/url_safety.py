"""出站 URL 安全校验（SSRF 防护，收尾修复第 5 批）。

所有由 Server 主动发起的出站 HTTP 请求（开放 API Webhook 投递、通知渠道发送）
在**配置创建/更新时**与**实际发送前**都必须经过本模块校验，防止攻击者借助
Server 请求内网、回环地址或云元数据接口（如 169.254.169.254）。

策略（fail-closed，任一不通过即拒绝）：

- 仅允许 ``http`` / ``https`` scheme；
- host 为 IP 字面量时直接判定（覆盖 IPv6 缩写、IPv4-mapped IPv6 如 ``::ffff:127.0.0.1``）；
- host 为域名时进行真实 DNS 解析（A/AAAA），**全部**解析结果都必须为公网地址
  ——任一结果指向内网即拒绝，防止公私混用的 A 记录绕过；
- DNS 解析失败一律拒绝；
- 拒绝范围：环回（127/8、::1）、私有（10/8、172.16/12、192.168/16、fc00::/7）、
  链路本地（169.254/16、fe80::/10，覆盖云元数据 169.254.169.254）、
  保留/未指定/组播地址，以及 ``localhost``（含带尾点变体 ``localhost.``）。

受信环境（如回调全部位于内网的私有化部署）可显式设置
``OUTBOUND_ALLOW_PRIVATE_HOSTS=true`` 放行私网地址，风险见
``docs/api/API_OPEN_DESIGN.md`` §12。

DNS rebinding 防护：创建时校验 + 投递前重新解析校验，且 HTTP 客户端不跟随
重定向（重定向是绕过 host 校验的经典路径）。详见 services/webhook.py 与
core/backends/* 的发送路径。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("core.url_safety")

# 按名称直接拦截的主机名（已做小写化与去尾点归一）
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset({"localhost"})

# 错误信息保持通用：明确告知被拒绝，但不回显解析到的内网地址等拓扑细节。
_MSG_SCHEME = "URL 必须以 http:// 或 https:// 开头"
_MSG_NO_HOST = "URL 缺少主机名"
_MSG_BLOCKED = "URL 指向本地或内网地址，已被出站安全策略拦截"
_MSG_UNRESOLVABLE = "URL 主机名无法解析"


class UrlSafetyError(ValueError):
    """出站 URL 未通过安全校验（继承 ValueError，复用现有 400/422 转换路径）。"""


def _resolve_host_ips(host: str) -> list[str]:
    """DNS 解析主机名，返回去重后的 IP 字符串列表。

    解析失败或结果为空一律抛 UrlSafetyError（fail-closed）。
    测试可 monkeypatch 本函数以固定解析结果。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError) as exc:
        raise UrlSafetyError(_MSG_UNRESOLVABLE) from exc
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise UrlSafetyError(_MSG_UNRESOLVABLE)
    return ips


def _is_blocked_ip(ip_str: str) -> bool:
    """判断 IP 字面量是否属于禁止出站访问的范围。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        # 无法解析为 IP（含非法字面量）→ 视为危险，fail-closed
        return True
    # IPv4-mapped IPv6（如 ::ffff:127.0.0.1）归一到 IPv4 再判定，
    # 避免被 IPv6 字面量形式绕过。
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def resolve_outbound_ips(url: str) -> list[str]:
    """校验 URL 并返回其解析出的公网 IP 列表；任一检查不通过即抛 UrlSafetyError。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UrlSafetyError(_MSG_SCHEME)
    host = parsed.hostname
    if not host:
        raise UrlSafetyError(_MSG_NO_HOST)
    normalized = host.strip().lower().rstrip(".")
    if normalized in _BLOCKED_HOSTNAMES:
        raise UrlSafetyError(_MSG_BLOCKED)

    # IP 字面量（含 IPv6 缩写/映射形式）无需 DNS，直接判定
    try:
        ipaddress.ip_address(normalized)
        is_literal = True
    except ValueError:
        is_literal = False

    if is_literal:
        if _is_blocked_ip(normalized):
            raise UrlSafetyError(_MSG_BLOCKED)
        return [normalized]

    # 域名 → 真实 DNS 解析；全部结果都必须安全
    ips = _resolve_host_ips(normalized)
    for resolved in ips:
        if _is_blocked_ip(resolved):
            logger.warning(
                f"出站 URL 解析结果命中内网/保留地址，已拦截: host={normalized}"
            )
            raise UrlSafetyError(_MSG_BLOCKED)
    return ips


def validate_outbound_url(url: str) -> str:
    """校验出站 URL 的 SSRF 安全性，通过则原样返回，否则抛 UrlSafetyError。

    ``OUTBOUND_ALLOW_PRIVATE_HOSTS=true`` 时跳过私网拦截（仅保留 scheme/host
    基本校验）——仅限完全受信的内网部署显式开启。
    """
    if settings.OUTBOUND_ALLOW_PRIVATE_HOSTS:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise UrlSafetyError(_MSG_SCHEME)
        if not parsed.hostname:
            raise UrlSafetyError(_MSG_NO_HOST)
        return url
    resolve_outbound_ips(url)
    return url
