"""Find the nearest qualifying disclosure without a prepared case file."""

import os
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from time import monotonic, sleep
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import exchange_calendars as calendars

from .market import daily_closes, filing_evidence
from .models import Case, PriceWindow


SEARCH_DAYS = 365
EASTERN = ZoneInfo("America/New_York")
SEC_COMPANIES = "https://www.sec.gov/files/company_tickers.json"
DOCUMENT_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.html?", re.IGNORECASE)
ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")


@dataclass(frozen=True)
class Company:
    ticker: str
    cik: int
    name: str


@dataclass(frozen=True)
class Disclosure:
    accession: str
    accepted: datetime
    form: str
    primary_document: str


def _name_key(value):
    words = re.findall(r"[a-z0-9]+", value.lower())
    suffixes = {"inc", "incorporated", "corporation", "corp", "ltd", "limited", "plc", "co", "company"}
    if words and words[0] == "the":
        words.pop(0)
    while len(words) > 1 and words[-1] in suffixes:
        words.pop()
    return "".join(words)


def resolve_company(rows, value):
    value = value.strip()
    if not value:
        raise ValueError("Enter a company name or US ticker")
    ticker = value.upper().replace(".", "-")
    matches = [row for row in rows if row["ticker"].upper().replace(".", "-") == ticker]
    if not matches:
        name = _name_key(value)
        matches = [row for row in rows if name and _name_key(row["title"]) == name]
    if len(matches) > 1:
        choices = ", ".join(sorted({row["ticker"] for row in matches}))
        raise ValueError(f"Company name is ambiguous. Use the ticker ({choices}).")
    if not matches:
        raise ValueError("Company was not found in the SEC ticker directory. Try its US ticker.")
    row = matches[0]
    return Company(row["ticker"], int(row["cik_str"]), row["title"])


class ExhibitIndex(HTMLParser):
    """Read press-release exhibit links from SEC's filing detail tables."""

    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        self.links = []
        self.row = None
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
            self.hrefs = []
        if tag == "a" and self.row is not None:
            href = dict(attrs).get("href") or ""
            self.hrefs.append(href)

    def handle_data(self, data):
        if self.row is not None:
            self.row.append(data)

    def handle_endtag(self, tag):
        if tag != "tr" or self.row is None:
            return
        label = " ".join(self.row).upper()
        if re.search(r"\bEX-99(?:\.\d+)?\b|\bPRESS RELEASE\b|\bEARNINGS RELEASE\b", label):
            for href in self.hrefs:
                parts = urlsplit(href)
                if parts.netloc or parts.scheme or parts.query or parts.fragment:
                    continue
                filename = parts.path.removeprefix(self.directory)
                if DOCUMENT_NAME.fullmatch(filename):
                    priority = 0 if any(word in label for word in ("PRESS RELEASE", "EARNINGS RELEASE")) else 1
                    self.links.append((priority, filename))
        self.row = None


class SecDisclosures:
    def __init__(self, client):
        self.client = client
        self.user_agent = os.getenv("SEC_USER_AGENT", "").strip() or "MAIA/0.1 (FTEC5660 academic research)"
        self.last_request = 0.0

    def get(self, url):
        sleep(max(0.0, 0.15 - (monotonic() - self.last_request)))
        self.last_request = monotonic()
        response = self.client.get(url, headers={"User-Agent": self.user_agent})
        if response.status_code == 403:
            raise RuntimeError("SEC refused access. Configure SEC_USER_AGENT with your organization/project and contact email.")
        return response.raise_for_status()

    def company(self, value):
        return resolve_company(self.get(SEC_COMPANIES).json().values(), value)

    def disclosures(self, company, start, end):
        data = self.get(f"https://data.sec.gov/submissions/CIK{company.cik:010d}.json").json()["filings"]
        groups = [data["recent"]]
        earliest_filing = (start - timedelta(days=3)).isoformat()
        latest_filing = (end + timedelta(days=3)).isoformat()
        for archive in data["files"]:
            if archive["filingFrom"] <= latest_filing and archive["filingTo"] >= earliest_filing:
                name = archive["name"]
                if not re.fullmatch(r"CIK[0-9]+-submissions-[0-9]+\.json", name):
                    raise ValueError("Unexpected SEC submissions archive name")
                groups.append(self.get(f"https://data.sec.gov/submissions/{name}").json())

        candidates = {}
        for rows in groups:
            for index, form in enumerate(rows["form"]):
                if form not in {"8-K", "6-K"}:
                    continue
                if not earliest_filing <= rows["filingDate"][index] <= latest_filing:
                    continue
                accession = rows["accessionNumber"][index]
                accepted = datetime.fromisoformat(rows["acceptanceDateTime"][index].replace("Z", "+00:00"))
                if not ACCESSION.fullmatch(accession) or accepted.utcoffset() is None:
                    raise ValueError("SEC returned an invalid accession or timestamp for a candidate disclosure")
                if start <= accepted.astimezone(EASTERN).date() <= end:
                    candidates[accession] = Disclosure(accession, accepted, form, rows["primaryDocument"][index])
        return list(candidates.values())

    def evidence(self, company, disclosure):
        directory = f"/Archives/edgar/data/{company.cik}/{disclosure.accession.replace('-', '')}/"
        base = "https://www.sec.gov" + directory
        index = ExhibitIndex(directory)
        index.feed(self.get(f"{base}{disclosure.accession}-index.html").text)
        exhibits = list(dict.fromkeys(filename for _, filename in sorted(index.links, key=lambda item: item[0])))
        documents = exhibits[:3]
        if DOCUMENT_NAME.fullmatch(disclosure.primary_document) and disclosure.primary_document not in documents:
            documents.append(disclosure.primary_document)
        evidence = []
        for number, document in enumerate(documents, 1):
            url = base + document
            evidence.extend(filing_evidence(self.get(url).text, company.ticker, url, disclosure.accepted, f"SEC{number}"))
        return evidence


def _market_sessions(start, end):
    # Give the calendar padding beyond the query's bounds, including holidays.
    exchange = calendars.get_calendar("XNYS", start=start - timedelta(days=10), end=end + timedelta(days=10))
    return [exchange.session_close(session).to_pydatetime()
            for session in exchange.sessions_in_range(start, end)]


def discover_case(client, company_input, target, assess, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    latest = now.astimezone(EASTERN).date()
    if target > latest:
        raise ValueError("Target date is in the future")
    start = target - timedelta(days=SEARCH_DAYS)
    end = min(target + timedelta(days=SEARCH_DAYS), latest)
    sec = SecDisclosures(client)
    company = sec.company(company_input)
    filings = sec.disclosures(company, start, end)
    filings.sort(key=lambda item: (
        abs((item.accepted.astimezone(EASTERN).date() - target).days), item.accepted, item.accession,
    ))
    audit = {
        "company": company.name, "ticker": company.ticker, "cik": company.cik,
        "target_date": target.isoformat(), "search_start": start.isoformat(), "search_end": end.isoformat(),
        "filing_count": len(filings), "candidates": [],
    }
    if not filings:
        return None, audit

    first_session = start - timedelta(days=10)
    last_session = min(latest, end + timedelta(days=10))
    sessions = _market_sessions(first_session, last_session)
    benchmark = "QQQ" if company.ticker == "SPY" else "SPY"
    stock_prices = daily_closes(client, company.ticker.replace(".", "-"), first_session, last_session)
    benchmark_prices = daily_closes(client, benchmark, first_session, last_session)

    for filing in filings:
        event_date = filing.accepted.astimezone(EASTERN).date()
        row = {"accession": filing.accession, "form": filing.form, "accepted_at": filing.accepted.isoformat(),
               "distance_days": abs((event_date - target).days)}
        audit["candidates"].append(row)
        after = bisect_right(sessions, filing.accepted)
        if after == 0 or after >= len(sessions) or sessions[after] + timedelta(minutes=20) > now:
            row["status"] = "waiting_for_close"
            continue
        before_at, after_at = sessions[after - 1:after + 1]
        before, after_date = (point.astimezone(EASTERN).date() for point in (before_at, after_at))
        row.update(price_start_at=before_at.isoformat(), price_end_at=after_at.isoformat())
        if before not in stock_prices or after_date not in stock_prices:
            row["status"] = "missing_stock_price"
            continue
        stock_start, stock_end = stock_prices[before], stock_prices[after_date]
        row["stock_return"] = stock_end / stock_start - 1
        if stock_end >= stock_start:
            row["status"] = "price_did_not_decline"
            continue
        if before not in benchmark_prices or after_date not in benchmark_prices:
            row["status"] = "missing_benchmark_price"
            continue
        evidence = sec.evidence(company, filing)
        if not evidence:
            row["status"] = "unreadable_filing"
            continue
        assessment = assess(company.ticker, evidence)
        if assessment.claim is None:
            row["status"] = "no_positive_disclosure"
            continue
        cited = next((item for item in evidence if item.id == assessment.evidence_id), None)
        quote = " ".join(assessment.quote.split())
        if cited is None or quote not in cited.text:
            raise ValueError(f"DeepSeek returned an unverifiable disclosure quote for {filing.accession}")
        row.update(status="selected", model_summary=assessment.claim, positive_quote=quote,
                   positive_evidence_id=cited.id, source=cited.url)
        ticker_key = re.sub(r"[^a-z0-9]+", "_", company.ticker.lower()).strip("_")
        return Case(
            case_id=f"{ticker_key}_{filing.accession}", ticker=company.ticker,
            event_at=filing.accepted, cutoff_at=after_at, positive_claim=quote,
            positive_evidence_ids=[cited.id], evidence=evidence,
            prices=PriceWindow(
                start_at=before_at, end_at=after_at, stock_start=stock_start, stock_end=stock_end,
                benchmark_start=benchmark_prices[before], benchmark_end=benchmark_prices[after_date],
                benchmark_ticker=benchmark, source="Yahoo Finance daily closes (Close column)",
            ),
        ), audit
    return None, audit
