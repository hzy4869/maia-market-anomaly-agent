import unittest
from pathlib import Path

from maia.graph import build_graph
from maia.models import Case, Conclusion, Hypothesis, HypothesisProposal, Investigation, SearchRequest
from maia.providers import LocalEvidenceStore
from maia.reasoning import DemoReasoner


FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "synthetic_earnings.json"


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.case = Case.model_validate_json(FIXTURE.read_text(encoding="utf-8"))

    def run_graph(self, reasoner=None, case=None, rounds=2):
        return build_graph(reasoner or DemoReasoner(), LocalEvidenceStore(), rounds).invoke(
            {"case": case or self.case, "investigation": Investigation()})

    def test_demo_excludes_future_evidence_and_preserves_trace(self):
        result = self.run_graph()
        self.assertEqual({e.id for e in result["evidence"]}, {"E1", "E2", "E3", "E4"})
        self.assertAlmostEqual(result["metrics"]["excess_return"], -0.03)
        self.assertTrue(any("E5" in x for x in result["audit"]["issues"]))
        self.assertTrue(any(x["node"] == "countercheck" for x in result["trace"]))
        self.assertEqual(result["hypothesis_origins"]["H2"]["trigger_ids"], ["E4"])

    def test_hypothesis_admission_and_follow_up(self):
        initial = Hypothesis(id="H1", explanation="Sector weakness.")
        cost = Hypothesis(id="H2", explanation="Cost pressure.")

        class RevisingReasoner:
            def investigate(self, case, evidence, metrics, round_number):
                if round_number == 0:
                    return Investigation(hypotheses=[initial], searches=[
                        SearchRequest(query="margin guidance", reason="Initial probe"),
                    ])
                if round_number == 2:
                    return Investigation(hypotheses=[initial, cost])

                def proposal(id_, explanation, trigger):
                    return HypothesisProposal(id=id_, explanation=explanation, trigger_ids=[trigger],
                                              support_test="Rising costs", weaken_test="Stable costs")

                return Investigation(new_hypotheses=[
                    proposal("H2", cost.explanation, "E2"),
                    proposal("H3", "SECTOR WEAKNESS!", "E2"),
                    proposal("H4", "A later event explains it.", "E5"),
                ], searches=[
                    SearchRequest(query="sector peers", reason="Check whether costs can explain the spread.",
                                  hypothesis_id="H2"),
                    SearchRequest(query="future story", reason="This proposal was rejected.", hypothesis_id="H4"),
                    SearchRequest(query="Margin guidance!", reason="Already searched.", hypothesis_id="H2"),
                ])

        result = self.run_graph(RevisingReasoner())
        self.assertEqual(set(result["hypothesis_origins"]), {"H1", "H2"})
        revision = next(step for step in result["trace"]
                        if step["node"] == "investigate" and step["round"] == 1)
        self.assertEqual([h["id"] for h in revision["output"]["hypotheses"]], ["H1", "H2"])
        self.assertIn("H3: duplicate new hypothesis", result["audit"]["issues"])
        self.assertIn("H4: trigger is not evidence newly retrieved in this round", result["audit"]["issues"])
        retrievals = [step for step in result["trace"] if step["node"] == "retrieve"]
        self.assertEqual(retrievals[1]["queries"], ["sector peers"])
        self.assertEqual(retrievals[1]["new_evidence_ids"], ["E4"])

    def test_unknown_citations_are_removed_and_loop_is_bounded(self):
        class BadReasoner:
            def investigate(self, *args):
                return Investigation(hypotheses=[Hypothesis(id="H", explanation="Unsupported",
                    supporting_ids=["FAKE"])], searches=[SearchRequest(query="margin", reason="test")])
        result = self.run_graph(BadReasoner(), rounds=1)
        self.assertEqual(result["round"], 1)
        self.assertEqual(result["investigation"].hypotheses[0].supporting_ids, [])
        self.assertFalse(result["audit"]["citation_ids_valid"])

    def test_no_price_decline_skips_reasoner(self):
        class MustNotRun:
            def investigate(self, *args):
                raise AssertionError("Reasoner should not run")
        case = self.case.model_copy(deep=True)
        case.prices.stock_end = 101
        result = self.run_graph(MustNotRun(), case)
        self.assertFalse(result["metrics"]["price_declined"])
        self.assertEqual(result["conclusion"].status, "not_applicable")

    def test_final_conclusion_rejects_invalid_references(self):
        for hypothesis_id, evidence_ids in [
            ("H9", ["E2"]), ("H1", ["E2", "E5"]), ("H1", ["E4"]), (None, ["E2"]),
        ]:
            with self.subTest(hypothesis_id=hypothesis_id, evidence_ids=evidence_ids):
                class InvalidConclusion(DemoReasoner):
                    def conclude(self, *args):
                        return Conclusion(status="inconclusive", hypothesis_id=hypothesis_id,
                                          evidence_ids=evidence_ids, summary="Claim.",
                                          key_gap="Historical expectations.")

                result = self.run_graph(InvalidConclusion())
                self.assertEqual(result["conclusion"].status, "inconclusive")
                self.assertIsNone(result["conclusion"].hypothesis_id)
                self.assertIn("Conclusion rejected: invalid hypothesis or evidence reference.",
                              result["audit"]["issues"])

    def test_single_call_baseline_skips_synthesis(self):
        class BaselineReasoner:
            def investigate(self, *args):
                return Investigation(hypotheses=[
                    Hypothesis(id="H1", explanation="Weaker guidance.", supporting_ids=["E2"])
                ])

            def conclude(self, *args):
                raise AssertionError("Baseline must not make a second model call")

        result = build_graph(BaselineReasoner(), LocalEvidenceStore(all_initial=True),
                             max_rounds=0, countercheck=False, synthesize=False).invoke(
            {"case": self.case, "investigation": Investigation()}
        )
        self.assertEqual(result["conclusion"].status, "not_applicable")
        self.assertEqual(result["investigation"].hypotheses[0].supporting_ids, ["E2"])

    def test_case_requires_timezone_and_honors_event_evidence_cutoff(self):
        raw = self.case.model_dump(mode="json")
        raw["event_at"] = "2026-10-20T16:30:00"
        with self.assertRaises(ValueError):
            Case.model_validate(raw)
        raw = self.case.model_dump(mode="json")
        raw["positive_evidence_ids"] = ["E5"]
        with self.assertRaises(ValueError):
            Case.model_validate(raw)


if __name__ == "__main__":
    unittest.main()
