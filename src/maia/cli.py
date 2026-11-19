import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .discovery import discover_case
from .graph import build_graph
from .market import EarningsEvent, build_market_case
from .models import Case, Investigation
from .providers import LocalEvidenceStore
from .reasoning import APIReasoner, DeepSeekReasoner, DemoReasoner
from .report import render_discovery, render_report


def save_run(root, run_id, state, report):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = root / f"{run_id}_{timestamp}"
    output.mkdir(parents=True, exist_ok=False)
    (output / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "report.md").write_text(report, encoding="utf-8")
    print(f"Report: {(output / 'report.md').resolve()}")


def main():
    parser = argparse.ArgumentParser(description="MAIA evidence-led investigation")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("case", type=Path, nargs="?", help="Prepared case JSON file")
    source.add_argument("--event", type=Path, help="Load US daily closes and a filing from an event JSON")
    source.add_argument("--company", help="Find a nearby disclosure for a US ticker or company name")
    parser.add_argument("--date", type=date.fromisoformat, help="Target date (YYYY-MM-DD) for --company")
    reasoners = {"deepseek": DeepSeekReasoner, "api": APIReasoner, "demo": DemoReasoner}
    parser.add_argument("--mode", choices=reasoners, default="deepseek",
                        help="Inference backend (default: deepseek)")
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--no-countercheck", action="store_true", help="Ablation: skip critique node")
    parser.add_argument("--baseline", action="store_true",
                        help="Run one model call with all eligible case evidence")
    parser.add_argument("--output", type=Path, default=Path("runs"))
    args = parser.parse_args()
    if (args.company is None) != (args.date is None):
        parser.error("--company and --date must be used together")
    if args.mode == "demo" and (args.event or args.baseline or args.company is not None):
        parser.error("Use a model API for --company, --event or --baseline")
    load_dotenv()
    try:
        reasoner = reasoners[args.mode]()
        discovery = None
        if args.company is not None:
            with httpx.Client(timeout=20, follow_redirects=False) as client:
                case, discovery = discover_case(client, args.company, args.date, reasoner.assess_disclosure)
            if case is None:
                save_run(args.output, f"discovery_{args.date.isoformat()}", {
                    "case": None, "discovery": discovery, "model_usage": reasoner.usage,
                    "config": {"mode": args.mode, "model": reasoner.model, "input": "company_and_date"},
                }, f"# MAIA Discovery: {discovery['ticker']}\n\n{render_discovery(discovery)}")
                return
        elif args.event:
            event = EarningsEvent.model_validate_json(args.event.read_text(encoding="utf-8-sig"))
            with httpx.Client(timeout=20, follow_redirects=False) as client:
                case = build_market_case(event, client)
        else:
            case = Case.model_validate_json(args.case.read_text(encoding="utf-8-sig"))
        max_rounds = 0 if args.baseline else args.max_rounds
        countercheck = not (args.baseline or args.no_countercheck)
        if isinstance(reasoner, APIReasoner):
            reasoner.allow_search = max_rounds > 0
        graph = build_graph(reasoner, LocalEvidenceStore(all_initial=args.baseline),
                            max_rounds, countercheck)
        result = graph.invoke({"case": case, "investigation": Investigation()},
                              config={"recursion_limit": 40})
    except Exception as exc:
        # Never dump API client configuration or environment variables.
        parser.exit(1, f"Investigation failed ({type(exc).__name__}): {str(exc)[:500]}\n")
    state = {
        "case": case.model_dump(mode="json"),
        "evidence": [e.model_dump(mode="json") for e in result["evidence"]],
        "investigation": result["investigation"].model_dump(mode="json"),
        "metrics": result["metrics"], "audit": result["audit"],
        "trace": result["trace"],
        "config": {"mode": args.mode, "max_rounds": max_rounds,
                   "countercheck": countercheck, "baseline": args.baseline,
                   "input": "company_and_date" if discovery else "live_market" if args.event else "prepared_case"},
    }
    if discovery:
        state["discovery"] = discovery
    model = getattr(reasoner, "model", None)
    if model:
        state["config"]["model"] = model
        state["model_usage"] = reasoner.usage
    save_run(args.output, case.case_id, state, render_report(result, model=model, discovery=discovery))


if __name__ == "__main__":
    main()
