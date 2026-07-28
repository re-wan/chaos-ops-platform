"""通用分页 Schema 与依赖。"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field, field_validator

T = TypeVar("T")

DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class PageParams(BaseModel):
    """通用分页查询参数。"""

    page: int = Field(default=DEFAULT_PAGE, ge=1, description="页码，从 1 开始")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"每页条数，最大 {MAX_PAGE_SIZE}",
    )

    @field_validator("page_size")
    @classmethod
    def clamp_page_size(cls, v: int) -> int:
        """确保 page_size 不超过上限。"""
        return min(v, MAX_PAGE_SIZE)


class Page(BaseModel, Generic[T]):
    """通用分页响应模型。"""

    items: list[T]
    total: int
    page: int
    page_size: int

    model_config = {"from_attributes": True}
