"""End-to-end eval runner.

Usage:
    python -m backend.eval.run_eval                 # base labels + perturbations, deterministic only
    python -m backend.eval.run_eval --judge         # also run LLM-as-judge
    python -m backend.eval.run_eval --skip-perturbations
    python -m backend.eval.run_eval --out report.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.models import Claim
from backend.agent.agent import review_claim
from backend.eval import scorers


ROOT = Path(__file__).resolve().parents[2]
CLAIMS_PATH = ROOT / "data" / "claims.json"
LABELS_PATH = ROOT / "backend" / "eval" / "labels.json"
PERTURB_PATH = ROOT / "backend" / "eval" / "perturbations.json"


def _score_one(case_id: str, claim_dict: dict, lbl: dict, kind: str, run_judge: bool) -> dict:
    claim = Claim(**claim_dict)
    print(f"[run] {case_id} ({claim.claim_type}, {claim.insurance_provider}, {kind})...", flush=True)
    result = review_claim(claim)

    case: dict = {"claim_id": case_id, "ambiguity": lbl.get("ambiguity"), "kind": kind}
    case.update(scorers.score_action(result, lbl["expected_action"]))
    case.update(scorers.score_rule_path(result, lbl["expected_rule_path"]))
    case.update(scorers.score_citations(result))
    case.update(scorers.score_expected_section(result, lbl.get("expected_section")))

    if run_judge:
        retrieved_block = next(
            (t.detail.get("hits", []) for t in result.trace if t.step == "rag.search"),
            [],
        )
        j = scorers.score_llm_judge(claim_dict, retrieved_block, result)
        if "judge" in j:
            case["judge"] = j["judge"]
        elif "judge_skipped" in j:
            case["judge_skipped"] = True
        else:
            case["judge_error"] = j
    return case


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", action="store_true", help="Run LLM-as-judge in addition to deterministic scorers")
    ap.add_argument("--out", default=str(ROOT / "backend" / "eval" / "last_run.json"))
    ap.add_argument("--skip-perturbations", action="store_true")
    args = ap.parse_args()

    claims = {c["claim_id"]: c for c in json.loads(CLAIMS_PATH.read_text())}
    labels = json.loads(LABELS_PATH.read_text())
    perturbations = [] if args.skip_perturbations else json.loads(PERTURB_PATH.read_text())

    per_case = []

    # Base labeled cases
    for lbl in labels:
        cid = lbl["claim_id"]
        if cid not in claims:
            print(f"[skip] {cid} not in claims.json")
            continue
        per_case.append(_score_one(cid, claims[cid], lbl, kind="base", run_judge=args.judge))

    # Perturbations: apply mutation to a base claim, then score
    for p in perturbations:
        base_id = p["base_claim_id"]
        if base_id not in claims:
            print(f"[skip] perturbation {p['perturbation_id']}: base {base_id} not found")
            continue
        mutated = {**claims[base_id], **p["mutation"]}
        mutated["claim_id"] = f"{base_id}:{p['perturbation_id']}"
        per_case.append(_score_one(
            mutated["claim_id"], mutated, p, kind="perturbation", run_judge=args.judge
        ))

    summary = scorers.aggregate(per_case)

    report = {"summary": summary, "cases": per_case}
    Path(args.out).write_text(json.dumps(report, indent=2))

    print("\n=== Summary ===")
    print(json.dumps(summary, indent=2))
    print(f"\nPer-case:")
    for c in per_case:
        status = "PASS" if c["action_match"] and c.get("rule_path_match") else "FAIL"
        judge = ""
        if "judge" in c:
            j = c["judge"]
            judge = f"  judge: cf={j.get('citation_faithfulness')}/rca={j.get('reason_citation_alignment')}/rcl={j.get('reason_claim_alignment')}"
        tag = f"[{c['kind']:12s}]"
        print(f"  [{status}] {tag} {c['claim_id']:32s} expected={c['expected']:32s} actual={c['actual']:32s}{judge}")
    print(f"\nWrote: {args.out}")


if __name__ == "__main__":
    main()
