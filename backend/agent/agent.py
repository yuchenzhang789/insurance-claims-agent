"""Claim-review agent: rule engine → RAG → Claude tool call → validation → Decision."""
from __future__ import annotations

import os
import re
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

from backend.models import Citation, Claim, Decision, ReviewResult, TraceStep
from backend.rules.engine import check_overrides
from backend.rag.retriever import get_retriever
from backend.agent.tools import TOOL_SCHEMAS, TOOL_NAMES
from backend.agent.prompts import SYSTEM_PROMPT, build_user_message, build_retrieval_query


MODEL = os.getenv("CLAIMS_AGENT_MODEL", "claude-sonnet-4-5")  # Sonnet 4.5+ for strong tool-use
MAX_TOKENS = 1024
RETRIEVAL_TOP_K = 5
LOW_CONFIDENCE_SCORE = 2.0  # BM25 threshold for "insufficient retrieval"


def _rule_trace_summary(trace: list[TraceStep]) -> str:
    lines = []
    for step in trace:
        if step.step == "rule.code_consistency":
            lines.append(f"- Code consistency: {step.detail['explanation']}")
        elif step.step == "rule.required_docs":
            missing = step.detail["missing"]
            if missing:
                lines.append(f"- Required docs: MISSING {missing}")
            else:
                lines.append(f"- Required docs: all present ({step.detail['required']})")
        elif step.step == "rule.inactive_plan":
            if step.detail["triggered"]:
                lines.append("- Member inactive: true (would be denied).")
            else:
                lines.append("- Member active: true.")
        elif step.step == "rule.high_amount":
            if step.detail["triggered"]:
                lines.append(f"- High amount: ${step.detail['amount']:.2f} > $5000 (escalated).")
            else:
                lines.append(f"- Claimed amount ${step.detail['amount']:.2f} within normal range.")
    return "\n".join(lines) if lines else "(no rule trace)"


def _verify_citation_excerpt(excerpt: str, retrieved: list[Citation]) -> tuple[bool, str | None]:
    """Check that the cited excerpt substring appears in at least one retrieved chunk.
    Whitespace/case-tolerant. Returns (is_valid, matched_section_or_none)."""
    if not excerpt:
        return False, None
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()
    needle = norm(excerpt)
    # Require a meaningful span (avoid matching tiny common words)
    if len(needle) < 20:
        return False, None
    for c in retrieved:
        hay = norm(c.excerpt)
        if needle in hay:
            return True, c.section
        # Try a looser match: first 60 chars of the needle
        if len(needle) >= 60 and needle[:60] in hay:
            return True, c.section
    return False, None


def _tool_use_to_decision(
    tool_name: str,
    tool_input: dict[str, Any],
    retrieved: list[Citation],
) -> tuple[Decision, dict]:
    """Convert a tool_use block into a Decision + validation report."""
    validation: dict = {"tool": tool_name, "issues": []}

    def _cite_list(raw: list[dict]) -> list[Citation]:
        cits = []
        for r in raw or []:
            section = r.get("section", "")
            excerpt = r.get("excerpt", "")
            ok, matched = _verify_citation_excerpt(excerpt, retrieved)
            if not ok:
                validation["issues"].append(
                    f"Cited excerpt not found in retrieved chunks: '{excerpt[:80]}...'"
                )
            cits.append(Citation(plan=retrieved[0].plan if retrieved else "", section=matched or section, excerpt=excerpt, score=1.0 if ok else 0.0))
        return cits

    reason = tool_input.get("reason", "")

    if tool_name == "approve_pre_authorization":
        return (
            Decision(action=tool_name, reason=reason, citations=_cite_list(tool_input.get("citations", []))),
            validation,
        )
    if tool_name == "approve_claim_payment":
        return (
            Decision(
                action=tool_name,
                reason=reason,
                citations=_cite_list(tool_input.get("citations", [])),
                payment_in_dollars=float(tool_input.get("payment_in_dollars", 0.0)),
            ),
            validation,
        )
    if tool_name == "deny_claim":
        return (
            Decision(action=tool_name, reason=reason, citations=_cite_list(tool_input.get("citations", []))),
            validation,
        )
    if tool_name == "request_missing_documentation":
        return (
            Decision(
                action=tool_name,
                reason=reason,
                requested_documents=tool_input.get("documents", []),
            ),
            validation,
        )
    if tool_name == "route_to_senior_reviewer":
        return (
            Decision(
                action=tool_name,
                reason=reason,
                citations=_cite_list(tool_input.get("citations", [])),
                routing_target="senior_medical_reviewer",
            ),
            validation,
        )
    if tool_name == "route_to_coding_review":
        return (
            Decision(action=tool_name, reason=reason, routing_target="coding_review_team"),
            validation,
        )

    validation["issues"].append(f"Unknown tool name: {tool_name}")
    return (
        Decision(
            action="route_to_senior_reviewer",
            reason=f"Agent returned an unrecognized tool. Escalating. (raw: {tool_name})",
            routing_target="senior_medical_reviewer",
        ),
        validation,
    )


RULE_GROUNDING_QUERIES = {
    "rule.inactive_plan": "eligibility enrollment termination member coverage ends",
    "rule.code_consistency": "claim submission coding accuracy procedure diagnosis",
    "rule.required_docs": "claim submission required documentation medical records",
    "rule.high_amount": "prior authorization high cost services medical review",
}


def _terminal_rule_step(rule_trace: list[TraceStep]) -> str | None:
    """Identify which rule.* step actually terminated the pipeline.
    The rule engine short-circuits on trigger, so the last rule.* step is it —
    but we confirm by checking the detail payload (different rules use different keys)."""
    if not rule_trace:
        return None
    last = rule_trace[-1]
    if not last.step.startswith("rule."):
        return None
    d = last.detail
    if last.step == "rule.inactive_plan" and d.get("triggered"):
        return last.step
    if last.step == "rule.code_consistency" and d.get("consistent") is False:
        return last.step
    if last.step == "rule.required_docs" and d.get("missing"):
        return last.step
    if last.step == "rule.high_amount" and d.get("triggered"):
        return last.step
    return None


def _ground_rule_decision(
    decision: Decision,
    claim: Claim,
    rule_trace: list[TraceStep],
    trace: list[TraceStep],
) -> Decision:
    """Attach a policy citation to a rule-engine terminal so every decision
    carries retrieved policy evidence, per the spec's Policy Grounding requirement.

    Uses a fixed query per rule — we're not asking the LLM to interpret anything,
    just anchoring the decision in plan documentation the user can verify.
    """
    rule_name = _terminal_rule_step(rule_trace)
    if rule_name is None:
        return decision
    query = RULE_GROUNDING_QUERIES.get(rule_name)
    if not query:
        return decision
    retriever = get_retriever()
    hits = retriever.search(claim.insurance_provider, query, top_k=1)
    trace.append(
        TraceStep(
            step="rag.rule_grounding",
            detail={
                "rule": rule_name,
                "query": query,
                "plan": claim.insurance_provider,
                "n_hits": len(hits),
                "top_score": hits[0].score if hits else 0.0,
            },
        )
    )
    if hits:
        # Top chunk attached verbatim — score reflects BM25 confidence. No LLM
        # in the loop, so there's no excerpt to verify against retrieval.
        return decision.model_copy(update={"citations": hits[:1]})
    return decision


_SUPPORTED_PROVIDERS = {"Blue Shield PPO", "Blue Shield EPO", "Blue Shield HMO"}
_INJECTION_PHRASES = ["ignore previous", "system:", "you are now", "disregard", "new instructions"]
_MEDICAL_ADVICE_PHRASES = ["you should take", "recommend treatment", "prescribe", "i recommend you"]


_DISCLAIMER = "policy-emulation decision, not medical advice"


def _guard_output(decision: Decision, trace: list[TraceStep]) -> Decision:
    """Step 6: Structural output guardrail — checks disclaimer, allowed action, no medical advice."""
    reason = decision.reason or ""
    reason_lower = reason.lower()

    disclaimer_present = _DISCLAIMER in reason_lower
    action_valid = decision.action in {
        "approve_pre_authorization", "approve_claim_payment", "deny_claim",
        "request_missing_documentation", "route_to_senior_reviewer", "route_to_coding_review",
    }
    medical_advice_detected = any(p in reason_lower for p in _MEDICAL_ADVICE_PHRASES)

    issues = []
    if not disclaimer_present:
        issues.append("disclaimer_missing")
    if not action_valid:
        issues.append("action_not_in_allowed_set")
    if medical_advice_detected:
        issues.append("medical_advice_detected")

    passed = action_valid and not medical_advice_detected
    trace.append(TraceStep(
        step="guard.output",
        detail={
            "disclaimer_present": disclaimer_present,
            "action_valid": action_valid,
            "medical_advice_detected": medical_advice_detected,
            "issues": issues,
            "passed": passed,
        },
    ))

    if not passed:
        return Decision(
            action="route_to_senior_reviewer",
            reason=(
                f"Output guardrail blocked the proposed decision ({decision.action}): "
                f"{', '.join(issues)}. Escalating for human review. "
                "This is a policy-emulation decision, not medical advice."
            ),
            routing_target="senior_medical_reviewer",
        )

    # Auto-append disclaimer if missing but otherwise clean
    if not disclaimer_present:
        decision = decision.model_copy(update={
            "reason": reason.rstrip() + " This is a policy-emulation decision, not medical advice."
        })

    return decision


def _validate_input(claim: Claim, trace: list[TraceStep]) -> None:
    """Step 0: Log that structured fields have been validated."""
    checks = {
        "claim_type_valid": claim.claim_type in {"preapproval", "standard", "appeal"},
        "member_status_valid": claim.member_status in {"active", "inactive"},
        "claimed_amount_non_negative": claim.claimed_amount >= 0,
        "required_fields_present": all([
            claim.insurance_provider,
            claim.diagnosis_code,
            claim.procedure_code,
        ]),
        "prior_denial_present_for_appeal": (
            bool(claim.prior_denial_reason) if claim.claim_type == "appeal" else None
        ),
    }
    trace.append(TraceStep(step="validate.input", detail=checks))


def _guard_input(claim: Claim, trace: list[TraceStep]) -> None:
    """Step 1: Log input guardrail checks (carrier scope, injection heuristic, amount sanity)."""
    provider_supported = claim.insurance_provider in _SUPPORTED_PROVIDERS
    notes_lower = (claim.provider_notes or "").lower()
    injection_detected = any(phrase in notes_lower for phrase in _INJECTION_PHRASES)
    amount_sane = 0 <= claim.claimed_amount < 1_000_000

    trace.append(TraceStep(
        step="guard.input",
        detail={
            "provider_supported": provider_supported,
            "injection_detected": injection_detected,
            "amount_sane": amount_sane,
            "passed": provider_supported and not injection_detected and amount_sane,
        },
    ))


def review_claim(claim: Claim, client: Anthropic | None = None) -> ReviewResult:
    """End-to-end pipeline for a single claim."""
    trace: list[TraceStep] = []

    # Step 0: Input validation
    _validate_input(claim, trace)

    # Step 1: Input guardrails
    _guard_input(claim, trace)

    # Step 2+: Rule engine
    terminal, rule_trace = check_overrides(claim)
    trace.extend(rule_trace)
    if terminal is not None:
        terminal = _ground_rule_decision(terminal, claim, rule_trace, trace)
        terminal = _guard_output(terminal, trace)
        return ReviewResult(claim_id=claim.claim_id, decision=terminal, trace=trace)

    # 2. Retrieval (per-plan namespaced)
    retriever = get_retriever()
    query = build_retrieval_query(claim)
    retrieved = retriever.search(claim.insurance_provider, query, top_k=RETRIEVAL_TOP_K)
    trace.append(
        TraceStep(
            step="rag.search",
            detail={
                "plan": claim.insurance_provider,
                "query": query,
                "n_hits": len(retrieved),
                "top_score": retrieved[0].score if retrieved else 0.0,
                "hits": [
                    {"section": c.section, "score": round(c.score, 3), "excerpt_preview": c.excerpt[:160]}
                    for c in retrieved
                ],
            },
        )
    )

    # 2b. Low-confidence retrieval guard
    if not retrieved or retrieved[0].score < LOW_CONFIDENCE_SCORE:
        d = Decision(
            action="route_to_senior_reviewer",
            reason=(
                f"Retrieved policy evidence for {claim.insurance_provider} is insufficient "
                f"(top BM25 score {retrieved[0].score if retrieved else 0:.2f} < {LOW_CONFIDENCE_SCORE}). "
                "Escalating for human review to avoid a decision without policy grounding. "
                "This is a policy-emulation decision, not medical advice."
            ),
            citations=retrieved,
            routing_target="senior_medical_reviewer",
        )
        trace.append(TraceStep(step="guard.low_confidence_retrieval", detail={"triggered": True}))
        d = _guard_output(d, trace)
        return ReviewResult(claim_id=claim.claim_id, decision=d, trace=trace)

    # 3. LLM tool call
    client = client or Anthropic()
    user_msg = build_user_message(claim, retrieved, _rule_trace_summary(rule_trace))
    trace.append(TraceStep(step="llm.request", detail={"model": MODEL, "user_msg_len": len(user_msg)}))

    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        tools=TOOL_SCHEMAS,
        tool_choice={"type": "any"},  # force a tool call
        messages=[{"role": "user", "content": user_msg}],
    )

    # Find the tool_use block
    tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_use is None:
        # Fallback: no tool call → escalate
        d = Decision(
            action="route_to_senior_reviewer",
            reason="Agent did not produce a tool call. Escalating.",
            routing_target="senior_medical_reviewer",
        )
        trace.append(TraceStep(step="llm.response", detail={"error": "no_tool_use", "stop_reason": resp.stop_reason}))
        return ReviewResult(claim_id=claim.claim_id, decision=d, trace=trace)

    trace.append(
        TraceStep(
            step="llm.response",
            detail={
                "tool_name": tool_use.name,
                "tool_input_preview": str(tool_use.input)[:400],
                "stop_reason": resp.stop_reason,
                "usage": {
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                },
            },
        )
    )

    if tool_use.name not in TOOL_NAMES:
        d = Decision(
            action="route_to_senior_reviewer",
            reason=f"Agent called unknown tool '{tool_use.name}'. Escalating.",
            routing_target="senior_medical_reviewer",
        )
        return ReviewResult(claim_id=claim.claim_id, decision=d, trace=trace)

    # 4. Validate + convert
    decision, validation = _tool_use_to_decision(tool_use.name, tool_use.input, retrieved)
    trace.append(TraceStep(step="validate.decision", detail=validation))

    # 5. Post-validation safety net: if it claims to cite policy but no citation validated, escalate.
    citation_needed_actions = {"approve_pre_authorization", "approve_claim_payment", "deny_claim"}
    if decision.action in citation_needed_actions:
        any_valid = any(c.score > 0 for c in decision.citations)
        if not any_valid:
            trace.append(TraceStep(step="guard.ungrounded_decision", detail={"triggered": True}))
            decision = Decision(
                action="route_to_senior_reviewer",
                reason=(
                    "Agent proposed a decision but no cited excerpt could be verified against "
                    f"retrieved policy chunks. Escalating. (Proposed: {decision.action}; "
                    f"reason was: {decision.reason[:200]})"
                ),
                citations=retrieved[:2],
                routing_target="senior_medical_reviewer",
            )

    decision = _guard_output(decision, trace)
    return ReviewResult(claim_id=claim.claim_id, decision=decision, trace=trace)
