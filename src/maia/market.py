"""Build a US earnings case from SEC filings and daily market closes."""

import os
from datetime import datetime, time, timedelta, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import httpx
from pydantic import AwareDatetime, Field, model_validator

from .models import Case, Evidence, PriceWindow, Record


class FilingReference(Record):
    cik: int = Field(gt=0, le=9_999_999_999)
    accession: str = Field(pattern=r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
    document: str = Field(pattern=r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*\.html?$")


class EarningsEvent(Record):
    case_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,11}$")
    benchmark: str = Field(default="SPY", pattern=r"^[A-Z][A-Z0-9.-]{0,11}$")
    start_at: AwareDatetime
    end_at: AwareDatetime
    cutoff_at: AwareDatetime
    positive_claim: str = Field(min_length=1)
    filing: FilingReference
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_window(self):
        if not self.start_at < self.end_at <= self.cutoff_at <= datetime.now(timezone.utc):
            raise ValueError("Require start < end <= cutoff <= now")
        start, end = self.session_times()
        if start.time() != time(16) or end.time() != time(16):
            raise ValueError("Use 16:00 America/New_York as the daily close observation time")
        if end.date() <= start.date() or end.date() - start.date() > timedelta(days=31):
            raise ValueError("Choose two US session close dates within 31 days")
        if self.benchmark == self.ticker:
            raise ValueError("Benchmark must differ from the stock")
        return self

    def session_times(self):
        eastern = ZoneInfo("America/New_York")
        return self.start_at.astimezone(eastern), self.end_at.astimezone(eastern)


class FilingText(HTMLParser):
    discarded_tags = {"script", "style", "head", "ix:hidden"}
    block_tags = {"p", "div", "br", "tr", "td", "th", "li", "h1", "h2", "h3", "h4"}

    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.discarded_tags:
            self.hidden += 1
        if not self.hidden and tag in self.block_tags:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.discarded_tags:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in self.block_tags:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _sec_filing(client, event, user_agent):
    filing = event.filing
    headers = {"User-Agent": user_agent}
    company = client.get(
        f"https://data.sec.gov/submissions/CIK{filing.cik:010d}.json", headers=headers
    ).raise_for_status().json()
    if event.ticker not in company["tickers"]:
        raise ValueError("Ticker does not match the SEC CIK")
    submissions = company["filings"]
    rows = submissions["recent"]
    if filing.accession not in rows["accessionNumber"]:
        start, end = (point.date().isoformat() for point in event.session_times())
        for archive in submissions["files"]:
            if archive["filingFrom"] <= end and archive["filingTo"] >= start:
                rows = client.get(
                    f"https://data.sec.gov/submissions/{archive['name']}", headers=headers
                ).raise_for_status().json()
                if filing.accession in rows["accessionNumber"]:
                    break
        else:
            raise ValueError("Accession not found in SEC submissions for this CIK and window")
    index = rows["accessionNumber"].index(filing.accession)
    if rows["form"][index] not in {"8-K", "6-K"}:
        raise ValueError("Choose an original 8-K or 6-K filing")
    if rows["form"][index] == "8-K" and "2.02" not in rows["items"][index].split(","):
        raise ValueError("The 8-K must report results of operations under Item 2.02")
    accepted = datetime.fromisoformat(rows["acceptanceDateTime"][index].replace("Z", "+00:00"))
    if accepted.utcoffset() is None or not event.start_at <= accepted <= event.end_at:
        raise ValueError("SEC filing acceptance must fall inside the price window")
    url = f"https://www.sec.gov/Archives/edgar/data/{filing.cik}/{filing.accession.replace('-', '')}/{filing.document}"
    page = client.get(url, headers=headers).raise_for_status()
    evidence = filing_evidence(page.text, event.ticker, url, accepted, "SEC")
    if not evidence:
        raise ValueError("SEC document did not contain usable filing text")
    return accepted, evidence


def filing_evidence(html, ticker, url, accepted, id_prefix):
    parser = FilingText()
    parser.feed(html)
    words = "".join(parser.parts).split()[:6000]
    if len(words) < 40:
        return []
    evidence = []
    for offset in range(0, len(words), 400):
        part = offset // 400 + 1
        evidence.append(Evidence(
            id=f"{id_prefix}-{part}", title=f"{ticker} SEC filing {url.rsplit('/', 1)[-1]} (part {part})",
            text=" ".join(words[offset:offset + 400]), source="SEC EDGAR",
            url=url, published_at=accepted, kind="filing",
        ))
    return evidence


def daily_closes(client, ticker, start, end):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    response = client.get(url, params={
        "period1": int(datetime.combine(start, time.min, timezone.utc).timestamp()),
        "period2": int(datetime.combine(end + timedelta(days=1), time.min, timezone.utc).timestamp()),
        "interval": "1d",
    }, headers={"User-Agent": "Mozilla/5.0"}).raise_for_status()
    results = response.json()["chart"]["result"]
    if not results:
        raise ValueError(f"No daily market data for {ticker}")
    data = results[0]
    if data["meta"]["currency"] != "USD" or data["meta"]["exchangeTimezoneName"] != "America/New_York":
        raise ValueError(f"{ticker} is outside the supported US market")
    return {datetime.fromtimestamp(timestamp, timezone.utc).date(): close
            for timestamp, close in zip(data["timestamp"], data["indicators"]["quote"][0]["close"])
            if close is not None}


def _daily_closes(client, ticker, start, end):
    closes = daily_closes(client, ticker, start, end)
    if start not in closes or end not in closes:
        raise ValueError(f"Missing daily close for {ticker}. Check both session dates.")
    return closes[start], closes[end]


def build_market_case(event: EarningsEvent, client: httpx.Client, sec_user_agent: str | None = None) -> Case:
    agent = sec_user_agent or os.getenv("SEC_USER_AGENT", "").strip()
    if not agent or "@" not in agent:
        raise ValueError("Set SEC_USER_AGENT to your project name and contact email")
    accepted, evidence = _sec_filing(client, event, agent)
    dates = tuple(point.date() for point in event.session_times())
    stock_start, stock_end = _daily_closes(client, event.ticker, *dates)
    benchmark_start, benchmark_end = _daily_closes(client, event.benchmark, *dates)
    return Case(
        case_id=event.case_id, ticker=event.ticker, event_at=accepted, cutoff_at=event.cutoff_at,
        positive_claim=event.positive_claim, positive_evidence_ids=[evidence[0].id],
        prices=PriceWindow(start_at=event.start_at, end_at=event.end_at, stock_start=stock_start,
                           stock_end=stock_end, benchmark_start=benchmark_start,
                           benchmark_end=benchmark_end, benchmark_ticker=event.benchmark,
                           source="Yahoo Finance daily closes (Close column)"),
        evidence=evidence + event.evidence,
    )
