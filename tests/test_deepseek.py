import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

from maia.cli import main


class DeepSeekIntegrationTests(unittest.TestCase):
    def test_cli_uses_deepseek_for_investigation_countercheck_and_conclusion(self):
        requests = []
        investigation = {"hypotheses": [
            {"id": "H1", "explanation": "Margin guidance needs checking."},
        ], "searches": [{"query": "margin guidance", "reason": "Check the forecast."}]}
        reviewed = {"hypotheses": [
            {"id": "H1", "explanation": "Margin guidance was weaker.", "supporting_ids": ["E2"]},
        ], "searches": []}
        proposal = {
            "id": "H2", "explanation": "Input costs contributed to the price decline.",
            "trigger_ids": ["E2"], "supporting_ids": ["E2"],
            "support_test": "Input cost increases explain the margin forecast revision.",
            "weaken_test": "Input costs were unchanged while margins fell.",
        }
        followup = {"query": "input cost breakdown", "reason": "Check the forecast assumptions.",
                    "hypothesis_id": "H2"}
        revised = {**reviewed, "new_hypotheses": [proposal], "searches": [followup]}
        final = {"hypotheses": reviewed["hypotheses"] + [{
            "id": "H2", "explanation": proposal["explanation"], "supporting_ids": ["E2"],
        }], "searches": [followup]}
        conclusion = {
            "status": "inconclusive", "hypothesis_id": "H2", "evidence_ids": ["E2"],
            "summary": "Management cited rising costs when lowering guidance, making input costs a useful lead. "
                       "Whether investors sold because of those costs remains uncertain.",
            "key_gap": "Dated market expectations for the margin forecast.",
        }
        replies = iter(["  ", json.dumps(investigation), json.dumps(investigation),
                        json.dumps(revised), json.dumps(final), json.dumps(conclusion)])

        def respond(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "deepseek-flash")
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(body["thinking"], {"type": "disabled"})
            payload = json.loads(body["messages"][1]["content"])
            self.assertNotIn("E5", {e["id"] for e in payload["evidence"]})
            requests.append(payload)
            return httpx.Response(200, json={"choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": next(replies)},
            }]})

        def client(**kwargs):
            return OpenAI(http_client=http_client, **kwargs)

        fixture = Path(__file__).resolve().parents[1] / "examples" / "synthetic_earnings.json"
        with tempfile.TemporaryDirectory() as output, httpx.Client(
            transport=httpx.MockTransport(respond)
        ) as http_client, patch.dict("os.environ", {
            "DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_MODEL": "",
        }), patch("maia.reasoning.OpenAI", side_effect=client), patch.object(
            sys, "argv", ["maia", str(fixture), "--output", output, "--max-rounds", "1"]
        ):
            main()
            state_file, = Path(output).glob("*/state.json")
            state = json.loads(state_file.read_text(encoding="utf-8"))
            report = state_file.with_name("report.md").read_text("utf-8")
            self.assertIn("Model: deepseek-flash", report)
            self.assertIn("New evidence that prompted it: E2", report)
            self.assertIn("H2: input cost breakdown", report)
            self.assertLess(report.index("## Conclusion"), report.index("## Candidate explanations"))
            self.assertIn("Best-supported hypothesis: H2", report)
            self.assertIn("Causal assessment: Inconclusive.", report)
            self.assertIn(conclusion["key_gap"], report)

        self.assertEqual(len(requests), 6)
        self.assertEqual(requests[0], requests[1])
        self.assertNotIn("hypotheses", requests[1])
        self.assertIn("hypotheses", requests[2])
        self.assertIn("E2", requests[3]["revision"]["new_evidence_ids"])
        self.assertEqual(requests[3]["revision"]["previous_hypotheses"][0]["id"], "H1")
        self.assertEqual(requests[4]["revision"]["hypothesis_origins"]["H2"]["trigger_ids"], ["E2"])
        self.assertEqual([h["id"] for h in requests[5]["hypotheses"]], ["H1", "H2"])
        self.assertEqual(state["config"]["mode"], "deepseek")
        self.assertEqual(state["investigation"]["hypotheses"][0]["supporting_ids"], ["E2"])
        self.assertEqual(state["hypothesis_origins"]["H2"]["trigger_ids"], ["E2"])
        self.assertEqual(state["audit"]["unexecuted_searches"], [followup])
        self.assertEqual(state["conclusion"], conclusion)
        self.assertEqual([step["node"] for step in state["trace"][-2:]], ["review", "conclude"])
        self.assertEqual(sum(step["node"] == "retrieve" for step in state["trace"]), 1)
