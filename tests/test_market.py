import json
import unittest
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import ValidationError

from maia.market import EarningsEvent, build_market_case


class MarketCaseTests(unittest.TestCase):
    example = Path(__file__).resolve().parents[1] / "examples" / "nvda_fy2025_q4_event.json"

    def test_builds_case_from_sec_and_matching_daily_closes(self):
        event = EarningsEvent.model_validate_json(self.example.read_text(encoding="utf-8"))
        requested = []

        def respond(request):
            requested.append(request.url.path)
            if request.url.host == "data.sec.gov":
                self.assertEqual(request.headers["User-Agent"], "MAIA test tester@example.com")
                return httpx.Response(200, json={"tickers": ["NVDA"], "filings": {"recent": {
                    "accessionNumber": [event.filing.accession],
                    "acceptanceDateTime": ["2025-02-26T21:21:32Z"],
                    "form": ["8-K"],
                    "items": ["2.02,9.01"],
                }}})
            if request.url.host == "www.sec.gov":
                self.assertTrue(request.url.path.endswith("/q4fy25pr.htm"))
                return httpx.Response(200, text=(
                    "<script>skip me</script><p>" + "Revenue increased this fiscal quarter. " * 100 + "</p>"
                ))
            self.assertEqual(request.url.host, "query1.finance.yahoo.com")
            self.assertEqual(request.url.params["interval"], "1d")
            prices = [131.28, 120.15] if request.url.path.endswith("/NVDA") else [594.54, 585.05]
            return httpx.Response(200, json={"chart": {"result": [{
                "meta": {"currency": "USD", "exchangeTimezoneName": "America/New_York"},
                "timestamp": [1740580200, 1740666600],
                "indicators": {"quote": [{"close": prices}]},
            }]}})

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            case = build_market_case(event, client, "MAIA test tester@example.com")
        self.assertEqual(len(requested), 4)
        self.assertEqual(case.event_at, datetime.fromisoformat("2025-02-26T21:21:32+00:00"))
        self.assertEqual(case.prices.stock_end, 120.15)
        self.assertEqual(case.prices.benchmark_end, 585.05)
        self.assertEqual(case.positive_evidence_ids, ["SEC-1"])
        self.assertEqual(case.evidence[0].published_at, case.event_at)
        self.assertEqual(case.evidence[-1].id, "E4")
        self.assertNotIn("skip me", case.evidence[0].text)

    def test_rejects_a_price_timestamp_before_the_daily_close(self):
        raw = json.loads(self.example.read_text(encoding="utf-8"))
        raw["end_at"] = "2025-02-27T15:55:00-05:00"
        with self.assertRaisesRegex(ValidationError, "daily close observation time"):
            EarningsEvent.model_validate(raw)
