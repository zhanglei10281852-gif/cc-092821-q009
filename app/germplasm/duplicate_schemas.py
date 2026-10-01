from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DuplicateScanRequest(BaseModel):
    min_score: float = Field(default=55.0, ge=0, le=100)
    actor: str = Field(min_length=1, max_length=100)


class CandidateDecision(BaseModel):
    action: str = Field(pattern="^(unrelated|defer|reopen)$")
    expected_version: int = Field(gt=0)
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="", max_length=500)


class MergePreviewRequest(BaseModel):
    kept_accession_id: int = Field(gt=0)
    expected_version: int = Field(gt=0)
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(default="", max_length=1000)
    # 标量冲突字段的逐项保留决定：{字段名: "kept"|"retired"} 或 {"choice": ...}
    field_decisions: dict[str, Any] = Field(default_factory=dict)
    # 护照冲突键的逐项保留决定：{护照键名: "kept"|"retired"}
    passport_key_decisions: dict[str, str] = Field(default_factory=dict)


class MergeExecuteRequest(BaseModel):
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=100)
    actor: str = Field(min_length=1, max_length=100)
