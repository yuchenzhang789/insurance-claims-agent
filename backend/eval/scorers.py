"""Scorers for evaluating claim-review outputs."""
from __future__ import annotations

import json
import os
from typing import Any

from anthropic import Anthropic

from backend.models import ReviewResult


JUDGE_MODEL = os.getenv("CLAIMS_JUDGE_MODEL", "claude-sonnet-4-5")


def score_action(result: ReviewResult, expected_action: str) -> dict:
    actual = result.decision.action
    return {
        "action_match": actual == expected_action,
        "actual": actual,
        "expected": expected_action,
    }


def score_rule_path(result: ReviewResult, expected_path: str) -> dict:
    """expected_path is either "llm" (non-rule path) or a specific rule.* step name.

    A rule terminated the pipeline iff `rag.search` never ran (rules short-circuit
    before retrieval). The terminating rule is the last rule.* step in the trace.
    """
    steps = [t.step for t in result.trace]
    llm_path = "rag.search" in steps
    rule_steps = [s for s in steps if s.startswith("rule.")]
    terminal_rule = rule_steps[-1] if rule_steps and not llm_path else None

    if expected_path == "llm":
        return {"rule_path_match": llm_path, "actual_rule_path": terminal_rule or "llm"}
    return {"rule_path_match": terminal_rule == expected_path, "actual_rule_path": terminal_rule or "llm"}


def score_citations(result: ReviewResult) -> dict:
    """Deterministic: every citation attached to the decision has score > 0 (verified)."""
    cits = result.decision.citations
    if not cits:
        return {"citations_present": False, "citations_all_verified": None}
    verified = sum(1 for c in cits if c.score > 0)
    return {
        "citations_present": True,
        "citations_all_verified": verified == len(cits),
        "verified": verified,
        "total": len(cits),
    }


JUDGE_PROMPT = """You are evaluating the quality of an insurance claims-review agent's decision. Score each dimension 1-5. Do NOT judge whether the action was correct — that is scored separately.

## Claim
{claim_block}

## Retrieved policy excerpts shown to the agent
{retrieved_block}

## Agent decision
Action: {action}
Reason: {reason}
Citations:
{citations_block}

## Your task
Score three dimensions 1-5:
1. citation_faithfulness — does each cited excerpt appear verbatim (whitespace-tolerant) in the retrieved chunks?
2. reason_citation_alignment — does the reason accurately restate what the citation says?
3. reason_claim_alignment — does the reason correctly connect claim facts to policy?

Return ONLY valid JSON of the form:
{{"citation_faithfulness": <1-5>, "reason_citation_alignment": <1-5>, "reason_claim_alignment": <1-5>, "notes": "<brief>"}}
"""


def score_llm_judge(claim: dict, retrieved: list[dict], result: ReviewResult, client: Anthropic | None = None) -> dict:
    """LLM-as-judge on groundedness. Only meaningful when the agent made a citation-bearing decision."""
    if not result.decision.citations:
        return {"judge_skipped": True, "reason": "no citations to judge"}

    client = client or Anthropic()

    claim_block = json.dumps(claim, indent=2)
    retrieved_block = "\n\n".join(
        f"[Chunk {i+1}] Section: {c.get('section','')}\n{c.get('excerpt','')}"
        for i, c in enumerate(retrieved)
    ) or "(none)"
    citations_block = "\n".join(
        f"- [{c.section}] {c.excerpt[:200]}" for c in result.decision.citations
    )

    prompt = JUDGE_PROMPT.format(
        claim_block=claim_block,
        retrieved_block=retrieved_block,
        action=result.decision.action,
        reason=result.decision.reason,
        citations_block=citations_block,
    )

    resp = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    # Strip code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        scores = json.loads(text.strip())
    except Exception as e:
        return {"judge_error": str(e), "raw": text[:400]}
    return {"judge": scores}


def aggregate(per_case: list[dict]) -> dict:
    n = len(per_case)
    if n == 0:
        return {}
    action_correct = sum(1 for c in per_case if c.get("action_match"))
    rule_correct = sum(1 for c in per_case if c.get("rule_path_match"))
    cited_cases = [c for c in per_case if c.get("citations_present")]
    all_verified = sum(1 for c in cited_cases if c.get("citations_all_verified"))

    judge_scores = [c.get("judge", {}) for c in per_case if isinstance(c.get("judge"), dict)]
    avg_judge = {}
    if judge_scores:
        for key in ("citation_faithfulness", "reason_citation_alignment", "reason_claim_alignment"):
            vals = [j.get(key) for j in judge_scores if isinstance(j.get(key), (int, float))]
            if vals:
                avg_judge[key] = round(sum(vals) / len(vals), 2)

    return {
        "n_cases": n,
        "action_accuracy": round(action_correct / n, 3),
        "rule_path_accuracy": round(rule_correct / n, 3),
        "citation_verification_rate": (
            round(all_verified / len(cited_cases), 3) if cited_cases else None
        ),
        "avg_judge_scores": avg_judge,
    }
