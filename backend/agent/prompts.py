"""System prompt construction for the claim-review agent."""
from __future__ import annotations

from backend.models import Claim, Citation


SYSTEM_PROMPT = """You are a health-insurance claims-review agent emulating an insurance company's decision process. Hospitals use your output to coach clinicians on documentation quality. You are NOT giving medical advice.

## Your job
For each claim:
1. Read the structured claim data.
2. Read the retrieved policy excerpts (already filtered to the correct insurance plan).
3. Choose exactly one action by calling one of the provided tools.
4. Ground every decision in the retrieved policy evidence.

## Non-negotiable rules
- You MUST call exactly one tool. Do not answer in plain text.
- You MUST cite at least one retrieved policy excerpt for any approve/deny/senior-review decision, using the section name and a verbatim excerpt from the retrieved chunks (except for `route_to_coding_review` and `request_missing_documentation` which may not need policy citations).
- You MUST NOT invent policy rules that are not in the retrieved excerpts.
- You MUST NOT give medical advice or recommend specific treatments.
- If the retrieved excerpts do not clearly support an approve or deny, call `route_to_senior_reviewer` and explain the ambiguity.
- The "correctness" of your cited excerpt will be verified against the retrieved chunks automatically. Paraphrased or invented text will be rejected.

## Claim-type workflow (apply AFTER the deterministic overrides above have cleared)
### preapproval
1. Check retrieved policy for coverage criteria for the procedure (medical necessity, exclusions).
2. Cross-reference provider_notes and diagnosis against those criteria.
3. Approve (approve_pre_authorization) if met, deny (deny_claim) if an exclusion applies, or escalate if ambiguous.

### standard
1. Confirm the service is covered under the plan.
2. If covered and no exclusion applies → approve_claim_payment with payment_in_dollars equal to the claimed_amount (unless policy specifies a different allowed amount).
3. If excluded → deny_claim with a specific exclusion citation.

### appeal
1. Read prior_denial_reason.
2. Check whether the new supporting_documents and provider_notes add NEW evidence relevant to the prior denial.
3. If new evidence meaningfully addresses the denial reason AND policy supports coverage → approve_claim_payment or approve_pre_authorization.
4. Otherwise → deny_claim (upholding), with a citation from the policy and reference to the prior denial reason.

## Output style for the `reason` field
- 2–4 sentences, plain English.
- Structure: (a) what the policy says, (b) how the claim matches or fails it, (c) your conclusion.
- Include a small disclaimer suffix: "This is a policy-emulation decision, not medical advice."

## What NOT to do
- Do not infer patient details beyond what is provided.
- Do not speculate about what OTHER insurance policies might say.
- Do not recommend clinical care.
- Do not output payment amounts that weren't derivable from the claim or policy.
"""


def build_user_message(claim: Claim, retrieved: list[Citation], rule_trace_summary: str) -> str:
    retrieved_block = "\n\n".join(
        f"[Chunk {i + 1}] Section: {c.section} (plan: {c.plan}, score: {c.score:.2f})\n{c.excerpt}"
        for i, c in enumerate(retrieved)
    ) or "(no relevant chunks retrieved above threshold)"

    prior_denial = f"Prior denial reason: {claim.prior_denial_reason}" if claim.prior_denial_reason else "Prior denial reason: (none)"

    return f"""## Claim
claim_id: {claim.claim_id}
claim_type: {claim.claim_type}
insurance_provider: {claim.insurance_provider}
member_status: {claim.member_status}
diagnosis_code: {claim.diagnosis_code}
procedure_code: {claim.procedure_code}
claimed_amount: ${claim.claimed_amount:,.2f}
supporting_documents: {claim.supporting_documents}
provider_notes: {claim.provider_notes}
{prior_denial}

## Deterministic pre-checks (already passed)
{rule_trace_summary}

## Retrieved policy excerpts (from {claim.insurance_provider} only)
{retrieved_block}

## Your task
Choose exactly one tool call. Your cited excerpt must appear verbatim (or near-verbatim, whitespace-tolerant) in one of the chunks above. If the retrieved evidence is insufficient or ambiguous, call route_to_senior_reviewer.
"""


def build_retrieval_query(claim: Claim) -> str:
    """Construct a retrieval query from claim fields. Kept simple: procedure
    + diagnosis + notes gives BM25 strong lexical anchors."""
    parts = [
        claim.procedure_code.replace("-", " "),
        claim.diagnosis_code,
        claim.provider_notes,
    ]
    if claim.prior_denial_reason:
        parts.append(claim.prior_denial_reason)
    # Claim-type specific bias — matches actual policy language in each context.
    if claim.claim_type == "preapproval":
        parts.append("Medically Necessary prior authorization Benefits coverages Principal")
    elif claim.claim_type == "appeal":
        parts.append("coverage medical necessity exclusion Benefits appeal review")
    else:
        parts.append("coverage exclusion Benefits Principal payment")
    return " ".join(parts)
