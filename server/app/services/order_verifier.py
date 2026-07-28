"""订单核验统一入口：本地人工订单优先，Paddle API 兜底。

核验顺序：
1. 查本地 manual_orders 表（人工收款录入的订单号）
2. 查不到 → 走 Paddle API 核验（自动收款）
3. 都没有 → 返回 False（统一错误「不匹配」）
"""

import logging

from sqlmodel import Session, select

from app.core import database as database_module
from app.models.manual_order import ManualOrder
from app.services import paddle_client

logger = logging.getLogger("chaosops.order_verifier")


def verify_order(order_id: str, email: str) -> bool:
    """核验订单是否真实有效且邮箱匹配（本地人工订单优先）。

    Returns:
        True 表示订单有效（本地人工订单或 Paddle 已支付订单）且邮箱匹配。

    Raises:
        paddle_client.PaddleUnavailableError: 仅当本地无记录且 Paddle 核验通道不可用时抛出。
    """
    # 1) 本地人工订单
    with Session(database_module.engine) as session:
        local = session.exec(
            select(ManualOrder).where(ManualOrder.order_id == order_id)
        ).first()
        if local is not None:
            matched = local.email.lower() == email.lower()
            if matched:
                logger.info(f"本地人工订单核验通过: order_id={order_id}")
            else:
                logger.info(f"本地人工订单邮箱不匹配: order_id={order_id}")
            return matched

    # 2) Paddle 自动收款订单
    return paddle_client.verify_order(order_id, email)
