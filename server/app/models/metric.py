"""指标数据模型。

本模块定义 Agent 上报与 Server 接收指标时使用的 Pydantic 模型。
指标实际存储在 InfluxDB，不进入关系型数据库。
"""

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.core.config import settings

# Prometheus 指标名合法字符：首字符 [a-zA-Z_:]，后续 [a-zA-Z0-9_:]*
_METRIC_NAME_PATTERN = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


class MetricSample(BaseModel):
    """单个指标样本。"""

    metric_name: str = Field(..., min_length=1, description="指标名")
    value: float = Field(..., description="指标值")
    timestamp: datetime = Field(..., description="指标采样时间（UTC）")
    labels: Optional[dict] = Field(default=None, description="标签键值对")

    @field_validator("metric_name")
    @classmethod
    def validate_metric_name(cls, v: str) -> str:
        """校验指标名格式。"""
        if not _METRIC_NAME_PATTERN.match(v):
            raise ValueError(
                "指标名格式非法，只允许 [a-zA-Z_:][a-zA-Z0-9_:]*"
            )
        return v


class MetricBatch(BaseModel):
    """Agent 批量上报请求体。"""

    node_id: str = Field(..., min_length=1, description="节点 ID")
    # max_length 限制单批样本数（DoS 防护）：超限在反序列化阶段直接 422，
    # 避免超大 payload 被物化到内存。默认 5000，经 METRIC_INGEST_MAX_SAMPLES 配置。
    samples: list[MetricSample] = Field(
        ...,
        max_length=settings.METRIC_INGEST_MAX_SAMPLES,
        description="指标样本列表",
    )


class IngestResponse(BaseModel):
    """指标上报响应体。"""

    accepted: int = Field(..., description="成功接收的样本数")
    dropped: int = Field(..., description="丢弃的样本数")
    # 批 14：pull 模型配置下发。仅当该节点存在 check_interval_override 覆盖键时
    # 返回，{metric_name: 间隔秒数}；老 Agent 忽略未知字段，不受影响。
    collector_intervals: Optional[dict[str, int]] = Field(
        default=None, description="采集器间隔覆盖（仅存在覆盖键时返回）"
    )
