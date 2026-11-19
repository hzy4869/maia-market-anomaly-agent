from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .models import Case, Evidence, Investigation
from .providers import EvidenceProvider
from .reasoning import Reasoner


class State(TypedDict, total=False):
    case: Case
    evidence: list[Evidence]
    metrics: dict
    investigation: Investigation
    round: int
    retrieved_new: bool
    warnings: list[str]
    trace: list[dict]
    audit: dict


def build_graph(reasoner: Reasoner, provider: EvidenceProvider, max_rounds: int = 2,
                countercheck: bool = True):
    if not 0 <= max_rounds <= 3:
        raise ValueError("max_rounds must be between 0 and 3")

    def accept(case, candidates, existing):
        kept = {e.id: e for e in existing}
        warnings = []
        for e in candidates:
            if e.published_at > case.cutoff_at:
                warnings.append(f"Excluded future evidence: {e.id}")
            elif e.id in kept and kept[e.id] != e:
                warnings.append(f"Conflicting evidence ID ignored: {e.id}")
            else:
                kept[e.id] = e
        return list(kept.values()), warnings

    def validate_event(s):
        c = s["case"]
        p = c.prices
        stock = p.stock_end / p.stock_start - 1
        benchmark = p.benchmark_end / p.benchmark_start - 1
        evidence, warnings = accept(c, provider.initial(c), [])
        return {"metrics": {"stock_return": stock, "benchmark_return": benchmark,
                            "excess_return": stock - benchmark,
                            "benchmark_ticker": p.benchmark_ticker,
                            "price_start_at": p.start_at.isoformat(),
                            "price_end_at": p.end_at.isoformat(),
                            "price_declined": stock < 0},
                "evidence": evidence, "warnings": warnings, "round": 0,
                "trace": [{"node": "validate_event", "evidence_ids": [e.id for e in evidence]}]}

    def investigate(s):
        result = reasoner.investigate(s["case"], s["evidence"], s["metrics"], s["round"])
        return {"investigation": result, "trace": s["trace"] + [{
            "node": "investigate", "round": s["round"],
            "output": result.model_dump(mode="json")} ]}

    def retrieve(s):
        candidates = []
        for request in s["investigation"].searches:
            candidates.extend(provider.search(s["case"], request.query))
        evidence, warnings = accept(s["case"], candidates, s["evidence"])
        return {"evidence": evidence, "warnings": s["warnings"] + warnings,
                "retrieved_new": len(evidence) > len(s["evidence"]),
                "round": s["round"] + 1, "trace": s["trace"] + [{
                    "node": "retrieve", "queries": [r.query for r in s["investigation"].searches],
                    "evidence_ids": [e.id for e in evidence]}]}

    def review(s):
        known = {e.id for e in s["evidence"]}
        issues = list(s["warnings"])
        hypotheses = []
        seen_ids = set()
        for original in s["investigation"].hypotheses:
            h = original.model_copy(deep=True)
            if h.id in seen_ids:
                issues.append(f"Duplicate hypothesis ID: {h.id}")
            seen_ids.add(h.id)
            unknown = (set(h.supporting_ids) | set(h.opposing_ids)) - known
            if unknown:
                issues.append(f"{h.id}: removed unknown citations {sorted(unknown)}")
            h.supporting_ids = sorted(set(h.supporting_ids) & known)
            h.opposing_ids = sorted(set(h.opposing_ids) & known)
            overlap = set(h.supporting_ids) & set(h.opposing_ids)
            if overlap:
                issues.append(f"{h.id}: same evidence cited on both sides {sorted(overlap)}")
            hypotheses.append(h)
        reviewed = Investigation(hypotheses=hypotheses)
        pending = bool(s["investigation"].searches)
        if s.get("retrieved_new") is False:
            issues.append("The final search found no new evidence available by the cutoff.")
        elif pending:
            issues.append("Search requests remain unexecuted at the investigation round limit.")
        if not countercheck:
            issues.append("Countercheck ablation: citation validation only; no semantic critique.")
        return {"investigation": reviewed, "audit": {
            "issues": issues, "citation_ids_valid": not any("unknown citations" in x for x in issues),
            "semantic_support_verified": False,
            "note": "Citation existence is checked in code; entailment and causality need human review."},
            "trace": s["trace"] + [{"node": "review", "issues": issues}]}

    def critique(s):
        # A distinct prompt sees hypotheses + evidence and can request disconfirming searches.
        # Demo behavior stays deterministic; API adapter implements semantic critique separately.
        if hasattr(reasoner, "critique"):
            result = reasoner.critique(s["case"], s["evidence"], s["metrics"], s["investigation"])
            return {"investigation": result, "trace": s["trace"] + [{
                "node": "countercheck", "output": result.model_dump(mode="json")} ]}
        return {"trace": s["trace"] + [{"node": "countercheck", "note": "Scripted demo; no LLM critique"}]}

    def route(s):
        return "retrieve" if s["investigation"].searches and s["round"] < max_rounds else "review"

    graph = StateGraph(State)
    for name, node in [("validate_event", validate_event), ("investigate", investigate),
                       ("retrieve", retrieve), ("countercheck", critique), ("review", review)]:
        graph.add_node(name, node)
    graph.add_edge(START, "validate_event")
    graph.add_conditional_edges("validate_event", lambda s: "investigate" if s["metrics"]["price_declined"] else "review")
    if countercheck:
        graph.add_edge("investigate", "countercheck")
        graph.add_conditional_edges("countercheck", route)
    else:
        graph.add_conditional_edges("investigate", route)
    graph.add_conditional_edges("retrieve", lambda s: "investigate" if s["retrieved_new"] else "review")
    graph.add_edge("review", END)
    return graph.compile()
