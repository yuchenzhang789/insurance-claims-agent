"""Pydantic types shared across modules."""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field


ClaimType = Literal["preapproval", "standard", "appeal"]
MemberStatus = Literal["active", "inactive"]
ActionLabel = Literal[
    "approve_pre_authorization",
    "approve_claim_payment",
    "deny_claim",
    "request_missing_documentation",
    "route_to_senior_reviewer",
    "route_to_coding_review",
]


class Claim(BaseModel):
    claim_id: str
    member_status: MemberStatus
    claim_type: ClaimType
    insurance_provider: str
    diagnosis_code: str
    procedure_code: str
    claimed_amount: float
    supporting_documents: list[str] = Field(default_factory=list)
    provider_notes: str = ""
    prior_denial_reason: Optional[str] = None


class Citation(BaseModel):
    plan: str
    section: str
    excerpt: str
    score: float = 0.0


class Decision(BaseModel):
    action: ActionLabel
    reason: str
    citations: list[Citation] = Field(default_factory=list)
    payment_in_dollars: Optional[float] = None
    requested_documents: Optional[list[str]] = None
    routing_target: Optional[str] = None


class TraceStep(BaseModel):
    step: str
    detail: dict


class ReviewResult(BaseModel):
    claim_id: str
    decision: Decision
    trace: list[TraceStep]
