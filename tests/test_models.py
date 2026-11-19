import unittest
from pathlib import Path

from maia.models import Case
from maia.providers import LocalEvidenceStore
from maia.reasoning import DemoReasoner


class OfflineComponentTests(unittest.TestCase):
    def setUp(self):
        fixture = Path(__file__).resolve().parents[1] / "examples" / "synthetic_earnings.json"
        self.case = Case.model_validate_json(fixture.read_text(encoding="utf-8"))

    def test_future_positive_claim_is_rejected(self):
        raw = self.case.model_dump(mode="json")
        raw["positive_evidence_ids"] = ["E5"]
        with self.assertRaises(ValueError):
            Case.model_validate(raw)

    def test_duplicate_ids_and_naive_times_are_rejected(self):
        raw = self.case.model_dump(mode="json")
        raw["evidence"].append(raw["evidence"][0])
        with self.assertRaises(ValueError):
            Case.model_validate(raw)
        raw = self.case.model_dump(mode="json")
        raw["event_at"] = "2026-10-20T16:30:00"
        with self.assertRaises(ValueError):
            Case.model_validate(raw)

    def test_retrieval_and_demo_reasoning_contract(self):
        provider = LocalEvidenceStore()
        initial = provider.initial(self.case)
        self.assertEqual([e.id for e in initial], ["E1"])
        first = DemoReasoner().investigate(self.case, initial, {}, 0)
        candidates = [e for q in first.searches for e in provider.search(self.case, q.query)]
        allowed = list({e.id: e for e in initial + candidates
                        if e.published_at <= self.case.cutoff_at}.values())
        final = DemoReasoner().investigate(self.case, allowed, {}, 1)
        self.assertEqual(final.hypotheses[0].supporting_ids, ["E2"])
        self.assertNotIn("E5", [e.id for e in allowed])

    def test_scripted_demo_rejects_real_cases(self):
        self.case.synthetic = False
        with self.assertRaises(ValueError):
            DemoReasoner().investigate(self.case, [], {}, 0)


if __name__ == "__main__":
    unittest.main()
