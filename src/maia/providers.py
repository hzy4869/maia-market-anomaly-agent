"""Tool boundary: replace LocalEvidenceStore with a timestamp-aware live adapter."""
import re
from typing import Protocol

from .models import Case, Evidence


class EvidenceProvider(Protocol):
    def initial(self, case: Case) -> list[Evidence]: ...
    def search(self, case: Case, query: str) -> list[Evidence]: ...


class LocalEvidenceStore:
    def __init__(self, all_initial: bool = False):
        self.all_initial = all_initial

    def initial(self, case: Case) -> list[Evidence]:
        if self.all_initial:
            return case.evidence.copy()
        wanted = set(case.positive_evidence_ids)
        return [e for e in case.evidence if e.id in wanted]

    def search(self, case: Case, query: str) -> list[Evidence]:
        # Deterministic offline retrieval; this is not web search or vector RAG.
        words = set(re.findall(r"\w+", query.lower()))
        scored = []
        for e in case.evidence:
            tokens = set(re.findall(r"\w+", (e.title + " " + e.text).lower()))
            score = len(words & tokens)
            if score:
                scored.append((score, e))
        return [e for _, e in sorted(scored, key=lambda x: (-x[0], x[1].id))[:5]]
