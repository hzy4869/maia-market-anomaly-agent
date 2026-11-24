import re
import unicodedata
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .models import MAX_ACTIVE_HYPOTHESES, MAX_INITIAL_HYPOTHESES, Case, Conclusion, Evidence, Hypothesis, Investigation
from .providers import EvidenceProvider
from .reasoning import Reasoner


class State(TypedDict, total=False):
    case: Case
    evidence: list[Evidence]
    metrics: dict
    investigation: Investigation
    round: int
    retrieved_new: bool
    new_evidence_ids: list[str]
    hypothesis_origins: dict[str, dict]
    searched_queries: list[str]
    warnings: list[str]
    trace: list[dict]
    audit: dict
    conclusion: Conclusion


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold()))


def _apply_investigation(s, result, node):
    round_number = s["round"]
    origins = dict(s["hypothesis_origins"])
    previous = {h.id: h for h in s["investigation"].hypotheses}
    retired = set(result.retired_hypothesis_ids) & previous.keys()
    active = {id_: h for id_, h in previous.items() if id_ not in retired} if round_number > 0 else {}
    added = []
    issues = []
    descriptions = {_normalized(origin["explanation"]) for origin in origins.values()}
    descriptions.update(_normalized(h.explanation) for h in previous.values())
    recent = set(s["new_evidence_ids"])
    seen_ids = set()

    for candidate in result.hypotheses:
        if candidate.id in retired:
            continue
        if candidate.id in seen_ids:
            issues.append(f"Duplicate hypothesis ID: {candidate.id}")
            continue
        seen_ids.add(candidate.id)
        description = _normalized(candidate.explanation)
        if not candidate.id.strip() or not description:
            issues.append("Skipped hypothesis without an ID or explanation")
            continue
        if candidate.id not in origins:
            if round_number > 0:
                issues.append(f"{candidate.id}: new hypothesis must be submitted with a trigger and tests")
                continue
            if description in descriptions:
                issues.append(f"{candidate.id}: duplicate hypothesis explanation")
                continue
        elif round_number > 0 and candidate.id not in previous:
            issues.append(f"{candidate.id}: cannot revise a retired hypothesis")
            continue
        if round_number == 0 and len(active) >= MAX_INITIAL_HYPOTHESES:
            issues.append(f"{candidate.id}: initial hypothesis limit reached")
            continue
        if candidate.id not in origins:
            origins[candidate.id] = {"round": 0, "node": node, "explanation": candidate.explanation}
        descriptions.add(description)
        active[candidate.id] = candidate

    for proposal in result.new_hypotheses:
        triggers = set(proposal.trigger_ids)
        description = _normalized(proposal.explanation)
        tests = {_normalized(proposal.support_test), _normalized(proposal.weaken_test)}
        if proposal.id in origins or description in descriptions:
            issues.append(f"{proposal.id}: duplicate new hypothesis")
        elif not triggers <= recent:
            issues.append(f"{proposal.id}: trigger is not evidence newly retrieved in this round")
        elif not proposal.id.strip() or not description or "" in tests or len(tests) != 2:
            issues.append(f"{proposal.id}: an ID, explanation and distinct support and weakening tests are required")
        elif len(active) >= MAX_ACTIVE_HYPOTHESES:
            issues.append(f"{proposal.id}: active hypothesis limit reached")
        else:
            origin = {"round": round_number, "node": node, "explanation": proposal.explanation,
                      "trigger_ids": sorted(triggers), "support_test": proposal.support_test,
                      "weaken_test": proposal.weaken_test}
            origins[proposal.id] = origin
            added.append(proposal.id)
            descriptions.add(description)
            active[proposal.id] = Hypothesis.model_validate(
                proposal.model_dump(include=set(Hypothesis.model_fields))
            )

    searches = []
    seen_queries = set(s["searched_queries"])
    for request in result.searches:
        key = _normalized(request.query)
        if (request.hypothesis_id is not None or round_number > 0) and request.hypothesis_id not in active:
            issues.append(f"Skipped search without an active hypothesis: {request.query}")
        elif not key or key in seen_queries:
            issues.append(f"Skipped empty or repeated search: {request.query}")
        else:
            searches.append(request)
            seen_queries.add(key)

    investigation = Investigation(hypotheses=list(active.values()), searches=searches)
    return {
        "investigation": investigation,
        "hypothesis_origins": origins,
        "warnings": s["warnings"] + issues,
        "trace": s["trace"] + [{"node": node, "round": round_number,
                               "added_hypothesis_ids": added,
                               "retired_hypothesis_ids": sorted(previous.keys() - active.keys()),
                               "issues": issues,
                               "output": investigation.model_dump(mode="json")}],
    }


def build_graph(reasoner: Reasoner, provider: EvidenceProvider, max_rounds: int = 2,
                countercheck: bool = True, *, synthesize: bool = True):
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
                "new_evidence_ids": [], "hypothesis_origins": {}, "searched_queries": [],
                "trace": [{"node": "validate_event", "evidence_ids": [e.id for e in evidence]}]}

    def revise(s, *, critique=False):
        return reasoner.revise(s["case"], s["evidence"], s["metrics"], s["round"],
                               s["investigation"], s["new_evidence_ids"], s["hypothesis_origins"],
                               critique=critique)

    def investigate(s):
        if s["round"] and hasattr(reasoner, "revise"):
            result = revise(s)
        else:
            result = reasoner.investigate(s["case"], s["evidence"], s["metrics"], s["round"])
        return _apply_investigation(s, result, "investigate")

    def retrieve(s):
        candidates = []
        for request in s["investigation"].searches:
            candidates.extend(provider.search(s["case"], request.query))
        evidence, warnings = accept(s["case"], candidates, s["evidence"])
        old_ids = {item.id for item in s["evidence"]}
        new_ids = [item.id for item in evidence if item.id not in old_ids]
        return {"evidence": evidence, "warnings": s["warnings"] + warnings,
                "retrieved_new": bool(new_ids), "new_evidence_ids": new_ids,
                "searched_queries": s["searched_queries"] + [_normalized(r.query) for r in s["investigation"].searches],
                "round": s["round"] + 1, "trace": s["trace"] + [{
                    "node": "retrieve", "queries": [r.query for r in s["investigation"].searches],
                    "requests": [r.model_dump(mode="json") for r in s["investigation"].searches],
                    "evidence_ids": [e.id for e in evidence], "new_evidence_ids": new_ids}]}

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
            "unexecuted_searches": [r.model_dump(mode="json") for r in s["investigation"].searches]
            if pending and s.get("retrieved_new") is not False else [],
            "note": "Citation existence is checked in code; entailment and causality need human review."},
            "trace": s["trace"] + [{"node": "review", "issues": issues}]}

    def critique(s):
        # A distinct prompt sees hypotheses + evidence and can request disconfirming searches.
        # Demo behavior stays deterministic; API adapter implements semantic critique separately.
        if hasattr(reasoner, "critique"):
            if s["round"] and hasattr(reasoner, "revise"):
                result = revise(s, critique=True)
            else:
                result = reasoner.critique(s["case"], s["evidence"], s["metrics"], s["investigation"])
            return _apply_investigation(s, result, "countercheck")
        return {"trace": s["trace"] + [{"node": "countercheck", "note": "Scripted demo; no LLM critique"}]}

    def conclude(s):
        investigation = s["investigation"]
        support_by_id = {h.id: set(h.supporting_ids) - set(h.opposing_ids)
                         for h in investigation.hypotheses}
        issue = None
        if not s["metrics"]["price_declined"]:
            conclusion = Conclusion(status="not_applicable",
                                    summary="No price decline occurred in the observation window.")
        elif not synthesize:
            conclusion = Conclusion(status="not_applicable",
                                    summary="The single-call baseline skips comparative synthesis.")
        elif not any(support_by_id.values()):
            conclusion = Conclusion(
                status="inconclusive",
                summary="No reviewed explanation has a citation listed as support alone.",
                key_gap="Contemporaneous evidence that could support or rule out a candidate explanation.",
            )
        else:
            try:
                conclusion = reasoner.conclude(
                    s["case"], s["evidence"], s["metrics"], investigation
                ) if hasattr(reasoner, "conclude") else None
            except RuntimeError:
                conclusion = None
                issue = "Conclusion synthesis failed; the reviewed hypotheses are retained."
            if conclusion is not None:
                known = {e.id for e in s["evidence"]}
                cited = set(conclusion.evidence_ids)
                if (conclusion.status == "not_applicable" or not cited <= known
                        or not cited.intersection(support_by_id.get(conclusion.hypothesis_id, ()))):
                    conclusion = None
                    issue = "Conclusion rejected: invalid hypothesis or evidence reference."
            if conclusion is None:
                conclusion = Conclusion(
                    status="inconclusive",
                    summary="No validated comparative conclusion is available. See the reviewed candidates.",
                    key_gap="Compare the leading candidates using their remaining evidence gaps.",
                )
        audit = s["audit"]
        if issue:
            audit = {**audit, "issues": audit["issues"] + [issue]}
        return {"conclusion": conclusion, "audit": audit, "trace": s["trace"] + [
            {"node": "conclude", "output": conclusion.model_dump(mode="json"), "issue": issue}
        ]}

    def route(s):
        return "retrieve" if s["investigation"].searches and s["round"] < max_rounds else "review"

    graph = StateGraph(State)
    for name, node in [("validate_event", validate_event), ("investigate", investigate),
                       ("retrieve", retrieve), ("countercheck", critique), ("review", review),
                       ("conclude", conclude)]:
        graph.add_node(name, node)
    graph.add_edge(START, "validate_event")
    graph.add_conditional_edges(
        "validate_event", lambda s: "investigate" if s["metrics"]["price_declined"] else "review",
        ["investigate", "review"],
    )
    if countercheck:
        graph.add_edge("investigate", "countercheck")
        graph.add_conditional_edges("countercheck", route, ["retrieve", "review"])
    else:
        graph.add_conditional_edges("investigate", route, ["retrieve", "review"])
    graph.add_conditional_edges(
        "retrieve", lambda s: "investigate" if s["retrieved_new"] else "review",
        ["investigate", "review"],
    )
    graph.add_edge("review", "conclude")
    graph.add_edge("conclude", END)
    return graph.compile()
