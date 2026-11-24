from .graph import State


def render_discovery(audit: dict) -> str:
    rows = audit["candidates"]
    selected = next((row for row in rows if row["status"] == "selected"), None)
    statuses = {
        "price_did_not_decline": "Price did not decline",
        "no_positive_disclosure": "No qualifying positive fact in retrieved documents",
        "missing_stock_price": "Missing stock close",
        "missing_benchmark_price": "Missing benchmark close",
        "waiting_for_close": "Post-disclosure close unavailable yet",
        "unreadable_filing": "No usable filing text",
        "selected": "Selected",
    }
    lines = ["## Company and date search", "",
             f"Company: {audit['company']} ({audit['ticker']})",
             f"Target date: {audit['target_date']} (New York)",
             f"Search range: {audit['search_start']} to {audit['search_end']}",
             f"Candidate SEC filings in range: {audit['filing_count']}", ""]
    if selected:
        lines += [f"Selected SEC filing {selected['accession']}, {selected['distance_days']} calendar day(s) from the target.",
                  f"Verified source excerpt ([SEC document]({selected['source']}))", "",
                  f"> {' '.join(selected['positive_quote'].split())}", ""]
    else:
        lines += ["No qualifying disclosure with an observed decline was found.", ""]
    if any(row["status"] in {"missing_stock_price", "missing_benchmark_price",
                             "waiting_for_close", "unreadable_filing"} for row in rows):
        lines += ["Some candidates could not be evaluated. See the statuses below.", ""]
    if rows:
        lines += ["| Accepted (UTC) | Accession | Days from target | Stock return | Result |",
                  "| --- | --- | ---: | ---: | --- |"]
        for row in rows:
            change = f"{row['stock_return']:.2%}" if "stock_return" in row else "—"
            lines.append(f"| {row['accepted_at']} | {row['accession']} | {row['distance_days']} | "
                         f"{change} | {statuses[row['status']]} |")
        lines.append("")
    return "\n".join(lines)


def render_report(state: State, model: str | None = None, discovery: dict | None = None) -> str:
    c, m = state["case"], state["metrics"]
    lines = [f"# MAIA Investigation: {c.ticker}", "",
             "SYNTHETIC CASE — fictional company, prices and evidence." if c.synthetic else "Research prototype — hypotheses require review.",
             "", f"Event: {c.event_at.isoformat()}", f"Information cutoff: {c.cutoff_at.isoformat()}",
             "", f"Positive claim ({'from the SEC disclosure' if discovery else 'user supplied'}): {c.positive_claim}",
             f"Claim sources: {', '.join(c.positive_evidence_ids)}",
             "", f"Stock return: {m['stock_return']:.2%}",
             f"Benchmark return: {m['benchmark_return']:.2%}",
             f"Return difference: {m['excess_return'] * 100:.2f} percentage points", ""]
    if model and m["price_declined"]:
        lines += [f"Model: {model}", ""]
    if c.prices.source:
        lines += [f"Price source: {c.prices.source}",
                  f"Benchmark: {c.prices.benchmark_ticker or 'unspecified'}",
                  f"Price window: {c.prices.start_at.isoformat()} to {c.prices.end_at.isoformat()}", ""]
    if conclusion := state.get("conclusion"):
        lines += ["## Conclusion", ""]
        if conclusion.hypothesis_id:
            lines += [f"**Best-supported hypothesis: {conclusion.hypothesis_id}**", ""]
        lines += [conclusion.summary, ""]
        if conclusion.status == "inconclusive":
            lines.append("**Causal assessment: Inconclusive.**")
        elif conclusion.status == "leading":
            lines.append("**Causal assessment: Provisional lead; causation is unverified.**")
        if conclusion.evidence_ids:
            lines.append(f"Evidence: {', '.join(conclusion.evidence_ids)}")
        if conclusion.key_gap:
            lines.append(f"Key evidence gap: {conclusion.key_gap}")
        lines.append("")
    if discovery:
        lines += [render_discovery(discovery), ""]
    if not m["price_declined"]:
        lines += ["Price did not decline in the supplied window; anomaly investigation stopped.", ""]
    lines += ["## Candidate explanations", ""]
    hypotheses = state["investigation"].hypotheses
    # Evidence-count ordering is transparent; it is not a calibrated confidence score.
    for h in sorted(hypotheses, key=lambda h: -len(h.supporting_ids)):
        lines += [f"### {h.id}: {h.explanation}", "",
                  "Evidence status: " + ("has cited support; requires review" if h.supporting_ids else "insufficient evidence"),
                  f"Supporting IDs: {', '.join(h.supporting_ids) or 'none'}",
                  f"Opposing IDs: {', '.join(h.opposing_ids) or 'none found; not proof of absence'}",
                  "Missing: " + ("; ".join(h.missing) or "not specified"), ""]
    discoveries = [(id_, origin) for id_, origin in state.get("hypothesis_origins", {}).items()
                   if origin["round"] > 0]
    if discoveries:
        active_ids = {h.id for h in hypotheses}
        lines += ["## Hypotheses raised by new evidence", ""]
        for id_, origin in discoveries:
            status = "retained for review" if id_ in active_ids else "removed from final candidates"
            lines += [f"### {id_}: {origin['explanation']}", "",
                      f"Introduced in round {origin['round']} ({origin['node']}); {status}.",
                      f"New evidence that prompted it: {', '.join(origin['trigger_ids'])}",
                      f"Would support it: {origin['support_test']}",
                      f"Would weaken it: {origin['weaken_test']}", ""]
    pending = state["audit"].get("unexecuted_searches", [])
    if pending:
        lines += ["## Follow-up searches left open", ""]
        lines.extend(f"- {r['hypothesis_id'] or 'General'}: {r['query']} ({r['reason']})" for r in pending)
        lines.append("")
    lines += ["## Evidence ledger", ""]
    for e in state["evidence"]:
        lines += [f"### {e.id}: {e.title}", "", f"Source: {e.source}; published {e.published_at.isoformat()}",
                  f"URL: {e.url or 'local fixture'}", "", e.text, ""]
    lines += ["## Review", "", state["audit"]["note"], ""]
    lines += [f"- {issue}" for issue in dict.fromkeys(state["audit"]["issues"])]
    return "\n".join(lines) + "\n"
