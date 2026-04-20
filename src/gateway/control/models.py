"""Pydantic input models for control-plane mutation endpoints."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AttestationUpsert(BaseModel):
    model_config = {"extra": "allow"}  # pass unknown fields to store as-is

    model_id: str
    attestation_id: str | None = None
    status: Literal["active", "revoked", "pending"] = "active"
    verification_level: str = "self_attested"
    tenant_id: str = ""
    provider: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyCreate(BaseModel):
    model_config = {"extra": "allow"}

    policy_name: str
    rules: list[dict[str, Any]] = Field(default_factory=list)
    enforcement_level: str = "blocking"
    tenant_id: str = ""
    version: int = 1


class PolicyUpdate(BaseModel):
    model_config = {"extra": "allow"}

    policy_name: str | None = None
    rules: list[dict[str, Any]] | None = None
    enforcement_level: str | None = None
    version: int | None = None


class BudgetUpsert(BaseModel):
    model_config = {"extra": "allow"}

    tenant_id: str = ""
    user_id: str = ""
    period: Literal["daily", "weekly", "monthly", "total"] = "monthly"
    max_tokens: int = Field(default=0, ge=0)
    cost_limit_usd: float | None = None
