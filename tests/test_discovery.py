import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import httpx

from maia.discovery import discover_case, resolve_company
from maia.models import PositiveDisclosure


class DiscoveryTests(unittest.TestCase):
    def test_scans_nearest_first_with_actual_session_closes(self):
        # July 3, 2024 closed early. An afternoon filing uses July 3 and July 5.
        filing_dates = ["2024-07-05", "2024-07-03", "2024-07-02", "2024-07-03"]
        accessions = [f"0001234567-24-{index:06d}" for index in (3, 2, 1, 4)]
        accessed = []
        assessed = []
        rows = {
            "form": ["8-K"] * 4, "filingDate": filing_dates, "accessionNumber": accessions,
            "acceptanceDateTime": [
                "2024-07-05T14:00:00Z", "2024-07-03T18:00:00Z",
                "2024-07-02T14:00:00Z", "2024-07-03T14:00:00Z",
            ],
            "primaryDocument": ["filing.htm"] * 4,
        }

        def respond(request):
            path = request.url.path
            if path.endswith("/company_tickers.json"):
                return httpx.Response(200, json={"0": {"ticker": "EXAM", "cik_str": 1234567, "title": "Example Corp"}})
            if request.url.host == "data.sec.gov":
                return httpx.Response(200, json={"filings": {"recent": rows, "files": []}})
            if path.endswith("-index.html"):
                accession = path.rsplit("/", 1)[-1].removesuffix("-index.html")
                accessed.append(accession)
                return httpx.Response(200, text='<table><tr><td>EX-99.1</td><td><a href="release.htm">Press release</a></td></tr></table>')
            if path.endswith("/release.htm") or path.endswith("/filing.htm"):
                message = ("Revenue increased by 25 percent." if accessions[2].replace("-", "") in path
                           else "The company announced a routine conference call.")
                return httpx.Response(200, text=f"<p>{message * 15}</p>")
            dates = [date(2024, 7, day) for day in (1, 2, 3, 5)]
            prices = [100, 98, 102, 95] if path.endswith("/EXAM") else [100, 99, 100, 99]
            return httpx.Response(200, json={"chart": {"result": [{
                "meta": {"currency": "USD", "exchangeTimezoneName": "America/New_York"},
                "timestamp": [datetime(d.year, d.month, d.day, 14, tzinfo=timezone.utc).timestamp() for d in dates],
                "indicators": {"quote": [{"close": prices}]},
            }]}})

        def assess(ticker, evidence):
            self.assertEqual(ticker, "EXAM")
            self.assertTrue(evidence[0].url.endswith("/release.htm"))
            assessed.append(evidence[0].url)
            quote = "Revenue increased by 25 percent."
            if quote in evidence[0].text:
                return PositiveDisclosure(claim="Revenue rose 25%.", evidence_id=evidence[0].id, quote=quote)
            return PositiveDisclosure(claim=None, evidence_id=None, quote=None)

        with httpx.Client(transport=httpx.MockTransport(respond)) as client, patch("maia.discovery.sleep"):
            case, audit = discover_case(client, "Example", date(2024, 7, 4), assess,
                                        now=datetime(2024, 7, 10, tzinfo=timezone.utc))
        self.assertEqual([row["status"] for row in audit["candidates"]],
                         ["price_did_not_decline", "no_positive_disclosure", "no_positive_disclosure", "selected"])
        self.assertEqual(audit["candidates"][1]["price_start_at"], "2024-07-03T17:00:00+00:00")
        self.assertEqual(audit["candidates"][1]["price_end_at"], "2024-07-05T20:00:00+00:00")
        self.assertEqual(accessed, [accessions[1], accessions[0], accessions[2]])
        self.assertEqual(len(assessed), 3)
        self.assertEqual(case.positive_claim, "Revenue increased by 25 percent.")
        self.assertEqual(case.prices.stock_start, 100)
        self.assertEqual(case.prices.stock_end, 98)

    def test_ambiguous_company_name_requires_share_class_ticker(self):
        rows = [{"ticker": ticker, "cik_str": 1, "title": "Example Inc"} for ticker in ("EXA", "EXB")]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            resolve_company(rows, "Example")
        self.assertEqual(resolve_company(rows, "exa").ticker, "EXA")
