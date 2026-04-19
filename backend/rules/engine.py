"""Deterministic rule engine — runs the Special Conditions overrides in
fixed precedence. Returns a terminal Decision if any override fires, else None."""
from __future__ import annotations

from typing import Optional

from backend.models import Claim, Decision, TraceStep
from backend.rules.codes import codes_consistent


# Required supporting documents per claim_type
REQUIRED_DOCS: dict[str, list[str]] = {
    "preapproval": ["clinical_note"],
    "standard": ["claim_form"],
    "appeal": ["appeal_letter"],
}

HIGH_AMOUNT_THRESHOLD = 5000.0


def check_overrides(
    claim: Claim,
) -> tuple[Optional[Decision], list[TraceStep]]:
    """Run overrides in precedence order. Return (terminal_decision | None, trace)."""
    trace: list[TraceStep] = []

    # 1. Inactive plan — hard stop
    if claim.member_status == "inactive":
        d = Decision(
            action="deny_claim",
            reason="Plan inactive at time of service.",
            citations=[],  # no policy lookup needed; this is a policy-independent rule from the spec
        )
        trace.append(TraceStep(step="rule.inactive_plan", detail={"triggered": True}))
        return d, trace
    trace.append(TraceStep(step="rule.inactive_plan", detail={"triggered": False}))

    # 2. Diagnosis / Procedure mismatch — coding review
    consistent, explanation = codes_consistent(claim.diagnosis_code, claim.procedure_code)
    trace.append(
        TraceStep(
            step="rule.code_consistency",
            detail={"consistent": consistent, "explanation": explanation},
        )
    )
    if not consistent:
        d = Decision(
            action="route_to_coding_review",
            reason=explanation,
            routing_target="coding_review_team",
        )
        return d, trace

    # 3. Missing essential documentation
    required = REQUIRED_DOCS.get(claim.claim_type, [])
    missing = [doc for doc in required if doc not in claim.supporting_documents]
    trace.append(
        TraceStep(
            step="rule.required_docs",
            detail={"required": required, "missing": missing},
        )
    )
    if missing:
        d = Decision(
            action="request_missing_documentation",
            reason=f"Required documents missing for {claim.claim_type} claim: {missing}. Marking as pending additional information.",
            requested_documents=missing,
        )
        return d, trace

    # 4. High claim amount — senior reviewer
    if claim.claimed_amount > HIGH_AMOUNT_THRESHOLD:
        d = Decision(
            action="route_to_senior_reviewer",
            reason=(
                f"Claimed amount ${claim.claimed_amount:,.2f} exceeds senior-review threshold "
                f"of ${HIGH_AMOUNT_THRESHOLD:,.2f}. Routing to a senior medical reviewer."
            ),
            routing_target="senior_medical_reviewer",
        )
        trace.append(TraceStep(step="rule.high_amount", detail={"triggered": True, "amount": claim.claimed_amount}))
        return d, trace
    trace.append(TraceStep(step="rule.high_amount", detail={"triggered": False, "amount": claim.claimed_amount}))

    # No override fires — let the LLM agent decide.
    return None, trace
