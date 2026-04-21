import unittest

from backend.eval import scorers
from backend.models import Citation, Decision, ReviewResult


class ScoreExpectedSectionTests(unittest.TestCase):
    def test_matches_when_any_citation_section_equals_expected(self) -> None:
        result = ReviewResult(
            claim_id="CLM1005",
            decision=Decision(
                action="approve_claim_payment",
                reason="Covered.",
                citations=[
                    Citation(plan="Blue Shield HMO", section="Emergency Services", excerpt="...", score=1.0),
                    Citation(plan="Blue Shield HMO", section="Principal Benefits", excerpt="...", score=1.0),
                ],
                payment_in_dollars=85.0,
            ),
            trace=[],
        )

        score = scorers.score_expected_section(result, "Principal Benefits")

        self.assertEqual(
            score,
            {
                "expected_section_present": True,
                "expected_section_match": True,
                "matched_section": "Principal Benefits",
            },
        )

    def test_reports_miss_when_expected_section_not_cited(self) -> None:
        result = ReviewResult(
            claim_id="CLM1001",
            decision=Decision(
                action="route_to_senior_reviewer",
                reason="Ambiguous.",
                citations=[
                    Citation(plan="Blue Shield PPO", section="Emergency Services", excerpt="...", score=1.0),
                ],
                routing_target="senior_medical_reviewer",
            ),
            trace=[],
        )

        score = scorers.score_expected_section(result, "Principal Benefits")

        self.assertEqual(
            score,
            {
                "expected_section_present": True,
                "expected_section_match": False,
                "matched_section": None,
            },
        )

    def test_skips_when_no_expected_section_is_labeled(self) -> None:
        result = ReviewResult(
            claim_id="CLM1002",
            decision=Decision(
                action="deny_claim",
                reason="Inactive.",
                citations=[
                    Citation(plan="Blue Shield EPO", section="Termination of Benefits", excerpt="...", score=8.2),
                ],
            ),
            trace=[],
        )

        score = scorers.score_expected_section(result, None)

        self.assertEqual(
            score,
            {
                "expected_section_present": False,
                "expected_section_match": None,
                "matched_section": None,
            },
        )


class AggregateTests(unittest.TestCase):
    def test_aggregate_reports_expected_section_accuracy(self) -> None:
        summary = scorers.aggregate(
            [
                {"action_match": True, "rule_path_match": True, "citations_present": True, "citations_all_verified": True, "kind": "base", "expected_section_present": True, "expected_section_match": True},
                {"action_match": True, "rule_path_match": True, "citations_present": True, "citations_all_verified": True, "kind": "base", "expected_section_present": True, "expected_section_match": False},
                {"action_match": True, "rule_path_match": True, "citations_present": True, "citations_all_verified": True, "kind": "perturbation", "expected_section_present": False, "expected_section_match": None},
            ]
        )

        self.assertEqual(summary["expected_section_accuracy"], 0.5)


class JudgeSkipTests(unittest.TestCase):
    def test_judge_skips_rule_terminal_even_if_grounding_citation_exists(self) -> None:
        result = ReviewResult(
            claim_id="CLM1002",
            decision=Decision(
                action="deny_claim",
                reason="Plan inactive at time of service.",
                citations=[
                    Citation(plan="Blue Shield EPO", section="Termination of Benefits", excerpt="...", score=7.5),
                ],
            ),
            trace=[],
        )

        score = scorers.score_llm_judge({}, [], result, client=None)

        self.assertEqual(
            score,
            {
                "judge_skipped": True,
                "reason": "rule-terminal decision with post-hoc grounding citation",
            },
        )


if __name__ == "__main__":
    unittest.main()
