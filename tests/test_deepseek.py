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
    def test_cli_uses_deepseek_for_investigation_and_countercheck(self):
        requests = []
        investigation = {"hypotheses": [
            {"id": "H1", "explanation": "Margin guidance needs checking."},
        ], "searches": [{"query": "margin guidance", "reason": "Check the forecast."}]}
        reviewed = {"hypotheses": [
            {"id": "H1", "explanation": "Margin guidance was weaker.", "supporting_ids": ["E2"]},
        ], "searches": []}
        replies = iter(["  ", json.dumps(investigation), json.dumps(investigation),
                        json.dumps(reviewed), json.dumps(reviewed)])

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
            self.assertIn("Model: deepseek-flash", state_file.with_name("report.md").read_text("utf-8"))

        self.assertEqual(len(requests), 5)
        self.assertEqual(requests[0], requests[1])
        self.assertNotIn("hypotheses", requests[1])
        self.assertIn("hypotheses", requests[2])
        self.assertEqual(state["config"]["mode"], "deepseek")
        self.assertEqual(state["investigation"]["hypotheses"][0]["supporting_ids"], ["E2"])
