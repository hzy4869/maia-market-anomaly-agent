from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Record):
    id: str = Field(min_length=1)
    title: str
    text: str = Field(min_length=1)
    source: str
    url: str = ""
    published_at: datetime
    kind: Literal["filing", "news", "market", "other"]


class PriceWindow(Record):
    start_at: datetime
    end_at: datetime
    stock_start: float = Field(gt=0)
    stock_end: float = Field(gt=0)
    benchmark_start: float = Field(gt=0)
    benchmark_end: float = Field(gt=0)
    benchmark_ticker: str | None = None
    source: str | None = None


class Case(Record):
    case_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    ticker: str
    event_at: datetime
    cutoff_at: datetime
    positive_claim: str
    positive_evidence_ids: list[str] = Field(min_length=1)
    synthetic: bool = False
    prices: PriceWindow
    evidence: list[Evidence]

    @model_validator(mode="after")
    def validate_case(self):
        timestamps = [self.event_at, self.cutoff_at, self.prices.start_at,
                      self.prices.end_at] + [e.published_at for e in self.evidence]
        if any(t.utcoffset() is None for t in timestamps):
            raise ValueError("All timestamps must include a timezone offset")
        if not self.prices.start_at <= self.event_at <= self.prices.end_at <= self.cutoff_at:
            raise ValueError("Require price start <= event <= price end <= cutoff")
        ids = [e.id for e in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("Evidence IDs must be unique")
        if not set(self.positive_evidence_ids) <= set(ids):
            raise ValueError("Positive claim references unknown evidence")
        if any(e.published_at > self.cutoff_at for e in self.evidence
               if e.id in self.positive_evidence_ids):
            raise ValueError("Positive claim cannot cite evidence after cutoff")
        return self


class Hypothesis(Record):
    id: str
    explanation: str
    supporting_ids: list[str] = Field(default_factory=list)
    opposing_ids: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class SearchRequest(Record):
    query: str = Field(min_length=1)
    reason: str


class Investigation(Record):
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=5)
    searches: list[SearchRequest] = Field(default_factory=list, max_length=3)


class PositiveDisclosure(Record):
    claim: str | None = Field(max_length=600)
    evidence_id: str | None
    quote: str | None = Field(max_length=900)

    @model_validator(mode="after")
    def check_citation(self):
        fields = (self.claim, self.evidence_id, self.quote)
        if not (all(value and value.strip() for value in fields) or all(value is None for value in fields)):
            raise ValueError("A positive claim requires both the evidence ID and exact quote")
        return self
