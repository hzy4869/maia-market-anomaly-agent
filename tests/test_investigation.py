import unittest
from pathlib import Path

from maia.graph import build_graph
from maia.models import Case, Hypothesis, Investigation, SearchRequest
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
        self.assertFalse(self.run_graph(MustNotRun(), case)["metrics"]["price_declined"])

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
