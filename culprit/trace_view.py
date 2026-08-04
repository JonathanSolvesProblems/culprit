"""Render the lineage walk as something a person can read in one glance.

The investigation's value is the path, not just the endpoint. A judge, or an
on-call engineer at 3am, needs to see the hops in between to decide whether the
dependency is real. This renders that path hop by hop.
"""

from __future__ import annotations

import time
from typing import Any

from rich.console import Console
from rich.text import Text

console = Console()


def _short(urn: str) -> str:
    """Turn a DataHub URN into something readable without losing identity."""
    if urn.startswith("urn:li:dataset:"):
        inner = urn.split(",")
        if len(inner) >= 2:
            return inner[1]
    if urn.startswith("urn:li:mlFeature:"):
        return "mlFeature: " + urn.rstrip(")").split(",")[-1]
    if urn.startswith("urn:li:mlFeatureTable:"):
        return "mlFeatureTable: " + urn.rstrip(")").split(",")[-1]
    if urn.startswith("urn:li:mlModel:"):
        parts = urn.rstrip(")").split(",")
        return "mlModel: " + (parts[1] if len(parts) > 1 else urn)
    if urn.startswith("urn:li:dataProcessInstance:"):
        return "trainingRun: " + urn.split(":")[-1]
    return urn


def render_lineage_path(
    root_dataset: str,
    root_column: str,
    hops: list[str],
    affected_features: list[str],
    model_name: str,
    trained_on: str | None = None,
    animate: bool = False,
    delay: float = 0.28,
) -> None:
    """Draw the path from the offending column down to the model.

    `animate` reveals one hop at a time, which is what the demo uses. It changes
    nothing about the content, only the pacing.
    """
    lines: list[Text] = []

    head = Text()
    head.append(f"{_short(root_dataset)}.", style="bold")
    head.append(root_column, style="bold red")
    head.append("   <- root cause", style="red")
    lines.append(head)

    indent = "  "
    for hop in hops:
        lines.append(Text(f"{indent}|", style="dim"))
        line = Text(f"{indent}+-- ")
        line.append(_short(hop), style="cyan")
        lines.append(line)
        indent += "     "

    for feature in affected_features:
        corrupt = Text(f"{indent}|  ")
        corrupt.append(feature, style="yellow")
        corrupt.append("  corrupted", style="dim yellow")
        lines.append(corrupt)

    lines.append(Text(f"{indent}|", style="dim"))
    model_line = Text(f"{indent}+-- ")
    model_line.append(model_name, style="bold red")
    lines.append(model_line)

    if trained_on:
        note = Text(f"{indent}     trained on: ")
        note.append(trained_on, style="bold")
        note.append("   <- never saw the new value", style="red")
        lines.append(note)

    console.print()
    for line in lines:
        console.print(line)
        if animate:
            time.sleep(delay)
    console.print()


def render_from_root_cause(
    rc: dict[str, Any],
    lineage_urns: list[str] | None = None,
    model_name: str | None = None,
    training_window: str | None = None,
    animate: bool = False,
) -> None:
    """Render from the agent's finding plus what it actually traversed.

    The hops come from tool calls made during the run. There is deliberately no
    hardcoded fallback path: if the agent never walked the lineage, the diagram
    says so rather than drawing a plausible-looking route it did not take.
    """
    hops = list(lineage_urns or [])
    if not hops:
        console.print(
            "[yellow]  (no lineage hops recorded during this run, "
            "so no path is drawn)[/yellow]"
        )

    render_lineage_path(
        root_dataset=rc.get("root_cause_dataset", "?"),
        root_column=rc.get("root_cause_column", "?"),
        hops=hops,
        affected_features=rc.get("affected_features", []),
        model_name=model_name or "the model under investigation",
        trained_on=training_window,
        animate=animate,
    )
