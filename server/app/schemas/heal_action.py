"""自愈动作请求/响应 Schema。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

VALID_ACTION_TYPES = {"builtin", "custom_script", "ai_generated_script"}
VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_INTERPRETERS = {"bash", "python", "python3"}


class HealActionCreate(BaseModel):
    """创建自定义动作请求体。"""

    action_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="唯一动作标识",
    )
    name: str = Field(..., min_length=1, max_length=128, description="动作名称")
    description: Optional[str] = Field(default=None, description="动作说明")
    action_type: str = Field(default="custom_script", description="动作类型")
    script_content: Optional[str] = Field(default=None, description="脚本内容")
    interpreter: str = Field(default="bash", description="脚本解释器: bash / python / python3")
    parameter_schema: dict = Field(default_factory=dict, description="参数 JSON Schema")
    risk_level: str = Field(default="medium", description="风险等级")

    @field_validator("action_type")
    @classmethod
    def validate_action_type(cls, v: str) -> str:
        if v not in VALID_ACTION_TYPES:
            raise ValueError(f"action_type 必须是 {VALID_ACTION_TYPES} 之一")
        return v

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, v: str) -> str:
        if v not in VALID_RISK_LEVELS:
            raise ValueError(f"risk_level 必须是 {VALID_RISK_LEVELS} 之一")
        return v

    @field_validator("interpreter")
    @classmethod
    def validate_interpreter(cls, v: str) -> str:
        if v not in VALID_INTERPRETERS:
            raise ValueError(f"interpreter 必须是 {VALID_INTERPRETERS} 之一")
        return v

    @field_validator("parameter_schema")
    @classmethod
    def validate_parameter_schema(cls, v: dict) -> dict:
        if not isinstance(v, dict):
            raise ValueError("parameter_schema 必须是 JSON 对象")
        if v.get("type") not in {"object", None}:
            raise ValueError("parameter_schema 根类型必须是 object")
        return v


class HealActionUpdate(BaseModel):
    """更新自定义动作请求体。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = None
    script_content: Optional[str] = None
    interpreter: Optional[str] = None
    parameter_schema: Optional[dict] = None
    risk_level: Optional[str] = None

    @field_validator("interpreter")
    @classmethod
    def validate_interpreter_update(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_INTERPRETERS:
            raise ValueError(f"interpreter 必须是 {VALID_INTERPRETERS} 之一")
        return v

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in VALID_RISK_LEVELS:
            raise ValueError(f"risk_level 必须是 {VALID_RISK_LEVELS} 之一")
        return v

    @field_validator("parameter_schema")
    @classmethod
    def validate_parameter_schema(cls, v: Optional[dict]) -> Optional[dict]:
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("parameter_schema 必须是 JSON 对象")
        if v.get("type") not in {"object", None}:
            raise ValueError("parameter_schema 根类型必须是 object")
        return v


class HealActionRead(BaseModel):
    """自愈动作响应模型。"""

    id: int
    action_id: str
    name: str
    description: Optional[str]
    action_type: str
    script_content: Optional[str]
    script_hash: Optional[str]
    interpreter: str
    parameter_schema: dict
    risk_level: str
    is_approved: bool
    is_builtin: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
