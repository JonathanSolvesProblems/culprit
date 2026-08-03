"""Culprit command line.

    python -m culprit.cli investigate
    python -m culprit.cli investigate --write-back
    python -m culprit.cli models
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from culprit import datahub_graph as dg
from culprit import trace_view
from culprit.agent import Investigation, TraceStep, investigate
from culprit.mcp_bridge import DataHubMCP
from culprit.writeback import write_back_all

console = Console()

DEFAULT_MODEL_URN = "urn:li:mlModel:(urn:li:dataPlatform:duckdb,nyc_fare_predictor,PROD)"
DEFAULT_SYMPTOM = (
    "Upfront fare quotes have drifted upward against settled fares over recent "
    "months. No pipeline has failed, no alert has fired, and the schema has not "
    "changed. Nobody knows why."
)
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _trace_markdown(inv: Investigation) -> str:
    lines = ["| # | tool | args | ms |", "|---|------|------|----|"]
    for step in inv.trace:
        args = json.dumps(step.arguments)
        args = args if len(args) <= 90 else args[:90] + "..."
        lines.append(f"| {step.index} | `{step.tool}` | `{args}` | {step.elapsed_ms} |")
    return "\n".join(lines)


def _render(inv: Investigation, animate: bool = False) -> None:
    rc = inv.root_cause
    if not rc:
        console.print(
            Panel(
                f"No root cause reported. Reason: {inv.stopped_reason}",
                title="Culprit", border_style="red",
            )
        )
        return

    if not rc.get("confident"):
        console.print(
            Panel(
                rc.get("headline", ""),
                title="Culprit: evidence insufficient for a confident diagnosis",
                border_style="yellow",
            )
        )

    console.print()
    console.print(Panel(rc.get("headline", ""), title="ROOT CAUSE", border_style="red"))

    trace_view.render_from_root_cause(rc, animate=animate)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("[bold]Dataset[/bold]", rc.get("root_cause_dataset", ""))
    table.add_row("[bold]Column[/bold]", rc.get("root_cause_column", ""))
    table.add_row("[bold]Affected inputs[/bold]", ", ".join(rc.get("affected_features", [])))
    console.print(table)

    console.print(Panel(rc.get("change_description", ""), title="What changed", border_style="cyan"))
    console.print(Panel(rc.get("mechanism", ""), title="Mechanism", border_style="cyan"))
    console.print(
        Panel(rc.get("why_monitors_missed_it", ""), title="Why every monitor stayed green",
              border_style="magenta")
    )

    impact = rc.get("impact", {}) or {}
    if impact:
        it = Table(show_header=True, header_style="bold")
        it.add_column("metric")
        it.add_column("value", justify="right")
        for key in (
            "affected_rows", "production_mae", "control_mae",
            "attributable_mae_per_row", "attributable_dollars", "gross_amount_exposed",
        ):
            if key in impact:
                value = impact[key]
                if key.endswith(("dollars", "exposed")) and isinstance(value, (int, float)):
                    value = f"${value:,.2f}"
                elif key == "affected_rows" and isinstance(value, (int, float)):
                    value = f"{int(value):,}"
                it.add_row(key, str(value))
        console.print(Panel(it, title="Measured impact (SQL, net of control)", border_style="green"))

    console.print(Panel(rc.get("recommended_fix", ""), title="Recommended fix", border_style="green"))

    if rc.get("proven"):
        console.print("\n[bold green]Proven from tool output[/bold green]")
        for claim in rc["proven"]:
            console.print(f"  [green]+[/green] {claim}")
    if rc.get("inferred"):
        console.print("\n[bold yellow]Inferred, not measured[/bold yellow]")
        for claim in rc["inferred"]:
            console.print(f"  [yellow]~[/yellow] {claim}")

    footer = (
        f"\n[dim]{len(inv.trace)} tool calls, {inv.turns} turns, "
        f"{inv.elapsed_seconds}s wall clock"
    )
    if inv.llm_model:
        footer += f" | {inv.llm_model}"
    if inv.input_tokens or inv.output_tokens:
        footer += f" | {inv.input_tokens:,} in / {inv.output_tokens:,} out tokens"
    if inv.estimated_cost_usd is not None:
        footer += f" | ~${inv.estimated_cost_usd:.3f}"
    console.print(footer + "[/dim]")


def cmd_models(_: argparse.Namespace) -> int:
    for model in dg.find_production_models():
        console.print(f"  {model['urn']}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Render a previously recorded real investigation.

    Included so a judge without an Anthropic API key can still see exactly what
    Culprit produced on the real data, and so the demo is reproducible frame for
    frame. This replays a recorded run; it does not re-derive anything.
    """
    path = EXAMPLES / "investigation.json"
    if not path.exists():
        console.print(
            Panel(
                f"No recorded investigation at {path}.\n"
                "Run `python -m culprit.cli investigate` first, or pull the one "
                "committed in examples/.",
                title="Nothing to replay", border_style="red",
            )
        )
        return 1

    payload = json.loads(path.read_text())
    console.print(
        Panel(
            payload.get("symptom", ""), title="Reported symptom", border_style="yellow"
        )
    )
    console.print(
        "\n[dim]Replaying a recorded run. "
        f"{payload.get('tool_calls', '?')} tool calls, "
        f"{payload.get('elapsed_seconds', '?')}s wall clock.[/dim]"
    )

    if args.animate:
        for step in payload.get("trace", []):
            args_preview = json.dumps(step.get("arguments", {}))
            if len(args_preview) > 70:
                args_preview = args_preview[:70] + "..."
            console.print(
                f"  [{step['index']:2d}] [cyan]{step['tool']}[/cyan]"
                f"({args_preview})  [dim]{step['elapsed_ms']}ms[/dim]"
            )
            time.sleep(0.18)

    inv = Investigation(
        model_urn=payload.get("model_urn", ""),
        root_cause=payload.get("root_cause"),
        elapsed_seconds=payload.get("elapsed_seconds", 0.0),
        turns=payload.get("turns", 0),
    )
    inv.trace = [
        TraceStep(
            index=s["index"], tool=s["tool"], arguments=s.get("arguments", {}),
            elapsed_ms=s.get("elapsed_ms", 0), result_preview=s.get("result_preview", ""),
        )
        for s in payload.get("trace", [])
    ]
    _render(inv, animate=args.animate)
    return 0


def cmd_investigate(args: argparse.Namespace) -> int:
    console.print(Panel(args.symptom, title="Reported symptom", border_style="yellow"))
    console.print("\n[bold]Investigating[/bold]\n")

    mcp = DataHubMCP()
    try:
        tools = mcp.start()
        console.print(f"[dim]DataHub MCP server: {len(tools)} tools available[/dim]\n")
        inv = investigate(args.model_urn, args.symptom, mcp=mcp, verbose=True)
        _render(inv, animate=args.animate)

        EXAMPLES.mkdir(exist_ok=True)
        payload = {
            "model_urn": inv.model_urn,
            "symptom": args.symptom,
            "root_cause": inv.root_cause,
            "elapsed_seconds": inv.elapsed_seconds,
            "turns": inv.turns,
            "tool_calls": len(inv.trace),
            "llm_model": inv.llm_model,
            "input_tokens": inv.input_tokens,
            "output_tokens": inv.output_tokens,
            "estimated_cost_usd": inv.estimated_cost_usd,
            "trace": [asdict(s) for s in inv.trace],
        }
        (EXAMPLES / "investigation.json").write_text(json.dumps(payload, indent=2, default=str))
        console.print(f"\n[dim]trace written to {EXAMPLES / 'investigation.json'}[/dim]")

        if args.write_back and inv.root_cause:
            console.print("\n[bold]Writing findings back to DataHub[/bold]")
            result = write_back_all(
                model_urn=inv.model_urn,
                root_cause=inv.root_cause,
                trace_markdown=_trace_markdown(inv),
                source_dataset_urn=args.source_dataset_urn,
                source_column=inv.root_cause.get("root_cause_column", ""),
                mcp=mcp,
            )
            for key, value in result.items():
                style = "red" if key.endswith("error") else "green"
                console.print(f"  [{style}]{key}[/{style}]: {str(value)[:200]}")
            (EXAMPLES / "writeback.json").write_text(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        mcp.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="culprit", description="A stack trace for model decay.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("models", help="List production models in DataHub").set_defaults(
        func=cmd_models
    )

    rep = sub.add_parser(
        "replay",
        help="Render a recorded real investigation. No API key needed.",
    )
    rep.add_argument(
        "--animate", action="store_true", help="Reveal the trace step by step"
    )
    rep.set_defaults(func=cmd_replay)

    inv = sub.add_parser("investigate", help="Diagnose a degraded model")
    inv.add_argument("--model-urn", default=DEFAULT_MODEL_URN)
    inv.add_argument("--symptom", default=DEFAULT_SYMPTOM)
    inv.add_argument("--write-back", action="store_true", help="Write findings into DataHub")
    inv.add_argument("--animate", action="store_true", help="Reveal the trace step by step")
    inv.add_argument(
        "--source-dataset-urn",
        default="urn:li:dataset:(urn:li:dataPlatform:duckdb,warehouse.raw.yellow_trips,PROD)",
        help="Dataset to annotate when writing back",
    )
    inv.set_defaults(func=cmd_investigate)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
