"""
产出物 Schema 标准化
参考架构文档 4.2: 5 个必填字段 + Pydantic 校验
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator
import uuid


class NextStep(BaseModel):
    """下一步行动"""
    action: str
    agent: str
    description: Optional[str] = None


class OutputSchema(BaseModel):
    """产出物标准 Schema — 5 个必填字段"""
    id: str
    type: str  # requirement / code / test_report / decision / ...
    created_at: datetime
    created_by: str  # agent name
    schema_version: str = "1.0"
    payload: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    next_steps: List[NextStep] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id 不能为空")
        return v

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_datetime(cls, v):
        if isinstance(v, str):
            return datetime.fromisoformat(v)
        return v

    def model_dump_json(self, *args, **kwargs) -> str:
        """确保 created_at 序列化为 ISO 格式"""
        return super().model_dump_json(*args, **kwargs)

    @classmethod
    def generate_id(cls, prefix: str = "task") -> str:
        """生成带前缀的唯一 ID"""
        short_id = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc)
        return f"{prefix}-{now.strftime('%Y%m%d')}-{short_id}"


class ValidationResult(BaseModel):
    """校验结果"""
    valid: bool
    errors: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class SchemaValidator:
    """Schema 校验门 — 确保产出物符合标准"""

    def __init__(self):
        self._validators: Dict[str, callable] = {}

    def register(self, output_type: str, validator: callable):
        self._validators[output_type] = validator

    def validate(self, output: OutputSchema) -> ValidationResult:
        errors = []
        warnings = []

        # 1. 基础 Schema 校验（Pydantic 已做）
        if not output.id:
            errors.append("id 字段不能为空")

        # 2. 类型特定校验
        if output.type in self._validators:
            try:
                result = self._validators[output.type](output)
                if isinstance(result, ValidationResult):
                    errors.extend(result.errors)
                    warnings.extend(result.warnings)
            except Exception as e:
                errors.append(f"自定义校验失败: {e}")

        # 3. next_steps 校验
        for step in output.next_steps:
            if not step.action:
                errors.append("next_steps 中的 action 不能为空")
            if not step.agent:
                errors.append("next_steps 中的 agent 不能为空")

        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )


# 全局校验器实例
validator = SchemaValidator()
