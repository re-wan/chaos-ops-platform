"""Paddle 订单核验客户端。

自助 License 生成前，通过 Paddle API 核验订单真实有效且邮箱匹配。
优雅降级（规格 §8）：

- 未配置 ``PADDLE_API_KEY`` → 核验「暂不可用」（抛 PaddleUnavailableError），
  端点返回「订单核验暂不可用，请稍后再试」，不生成、不报错给攻击面；
- 网络异常 / 非预期响应码 → 同样按不可用降级；
- 订单不存在 / 未支付 / 邮箱不匹配 → 返回 False，由端点统一映射为
  「订单号、邮箱或 install_id 不匹配」（防枚举，不区分失败原因）。
"""

import httpx

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger("services.paddle_client")

# 视为「已付款」的交易状态（Paddle Billing Transaction.status）
_PAID_STATUSES = {"paid", "completed"}


class PaddleUnavailableError(Exception):
    """Paddle 核验暂不可用（未配置 Key / 网络故障 / 上游 5xx 等）。"""


def verify_order(order_id: str, email: str) -> bool:
    """核验订单是否真实、已付款且邮箱匹配。

    Returns:
        True 表示订单有效且邮箱匹配；False 表示订单不存在/未支付/邮箱不匹配。

    Raises:
        PaddleUnavailableError: 核验通道不可用（调用方按「暂不可用」降级）。
    """
    api_key = settings.PADDLE_API_KEY
    if not api_key:
        logger.warning("PADDLE_API_KEY 未配置，订单核验暂不可用")
        raise PaddleUnavailableError("PADDLE_API_KEY 未配置")

    base_url = settings.PADDLE_API_BASE_URL.rstrip("/")
    try:
        response = httpx.get(
            f"{base_url}/transactions/{order_id}",
            params={"include": "customer"},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.warning(f"Paddle API 请求失败（降级为暂不可用）: {exc}")
        raise PaddleUnavailableError(str(exc)) from exc

    if response.status_code == 404:
        return False
    if response.status_code != 200:
        logger.warning(
            f"Paddle API 返回非预期状态码 {response.status_code}（降级为暂不可用）"
        )
        raise PaddleUnavailableError(f"Paddle API 状态码 {response.status_code}")

    try:
        data = response.json().get("data") or {}
    except ValueError as exc:
        logger.warning(f"Paddle API 响应不是合法 JSON（降级为暂不可用）: {exc}")
        raise PaddleUnavailableError("Paddle API 响应解析失败") from exc

    if data.get("status") not in _PAID_STATUSES:
        return False
    customer = data.get("customer") or {}
    customer_email = (customer.get("email") or "").strip().lower()
    if not customer_email:
        # 拿不到客户邮箱无法完成比对，按不可用降级而不是放行
        logger.warning("Paddle 交易缺少 customer.email（降级为暂不可用）")
        raise PaddleUnavailableError("Paddle 交易缺少客户邮箱")
    return customer_email == email.strip().lower()
