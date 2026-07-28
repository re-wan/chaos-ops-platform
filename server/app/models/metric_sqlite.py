"""SQLite 指标样本关系型表模型。

SQLiteMetricBackend 使用该表存储指标样本，
由 SQLModel.metadata 统一创建，索引加速查询。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class SQLiteMetricSample(SQLModel, table=True):
    """SQLite 指标样本表。"""

    __tablename__ = "metric_samples"

    __table_args__ = (
        Index(
            "idx_metric_samples_lookup",
            "node_id",
            "metric_name",
            "timestamp",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    node_id: str = Field(description="节点 ID")
    metric_name: str = Field(description="指标名")
    value: float = Field(description="指标值")
    timestamp: datetime = Field(description="采样时间（UTC）")
    labels_json: Optional[str] = Field(default=None, description="标签 JSON 字符串")
    created_at: Optional[datetime] = Field(
        default=None,
        description="记录创建时间",
    )
