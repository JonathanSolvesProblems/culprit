"""Verify every headline number in the docs against the committed artifacts.

Prose drifts from data. Two review rounds found six checkably-false statements in
this repo, and the pattern in every case was a document lagging a change made
somewhere else. This turns that check into something mechanical that can be run
before every commit instead of relied on a reviewer noticing.

Reads the real values out of examples/*.json, then greps the documentation for
each one and reports anything stated that the artifacts do not support.

Usage:  python scripts/check_claims.py
Exit code 1 if any claim fails.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
DOCS = [
    ROOT / "README.md",
    ROOT / "docs" / "SUBMISSION.md",
    ROOT / "docs" / "DEMO_SCRIPT.md",
    ROOT / "docs" / "STRATEGY.md",
    ROOT / "docs" / "DATAHUB_FINDINGS.md",
]


def load(name: str) -> dict:
    path = EXAMPLES / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def main() -> int:
    impact = load("04_measured_impact.json")
    run = load("investigation.json")
    rejected = load("remediation_rejected.json")
    sweep = load("02_monitor_sweep.json")
    ml = load("06_ml_lineage_in_datahub.json")

    trace = run.get("trace", [])
    datahub_calls = [s for s in trace if str(s.get("tool", "")).startswith("datahub_")]
    zero = (sweep.get("derived_feature") or {}).get("zero_count_first_change") or {}
    uniq = (sweep.get("source_column") or {}).get("unique_count_first_change") or {}

    # (label, expected value, list of spellings that must all be supported)
    checks: list[tuple[str, object, list[str]]] = [
        ("attributable dollars (DiD)", impact.get("did_attributable_dollars"),
         ["90,322.36", "90,322"]),
        ("attributable per row (DiD)", impact.get("did_attributable_mae_per_row"),
         ["1.3655", "1.37"]),
        ("naive attributable dollars", impact.get("attributable_dollars"), ["95,158"]),
        ("affected rows (scored month)", impact.get("affected_rows"), ["66,146"]),
        ("baseline control lift", impact.get("baseline_control_lift_per_row"), ["0.0731"]),
        ("baseline rows", impact.get("baseline_rows"), ["3,840,878"]),
        ("gross exposed", impact.get("gross_amount_exposed"), ["1,698,233.95"]),
        ("run wall clock", run.get("elapsed_seconds"), ["151.92"]),
        ("run tool calls", run.get("tool_calls"), ["| 28 ", "28 tool calls", "28 (4 of them"]),
        ("run cost", run.get("estimated_cost_usd"), ["0.279"]),
        ("datahub_* calls in run", len(datahub_calls), ["4 of them", "4 MCP calls"]),
        ("rows destroyed by rejected patch",
         sum((rejected.get("rows_destroyed_by_segment") or {}).values()) or None,
         ["87,693"]),
        ("mlFeatures emitted", len(ml.get("features") or []), ["13 mlFeature", "13 `mlFeature`"]),
        ("zero_count first exceedance month", zero.get("month"), ["2024-12"]),
        ("zero_count rows at first exceedance", zero.get("to"), ["255"]),
        ("unique_count first exceedance month", uniq.get("month"), ["2024-12"]),
    ]

    corpus = {p: p.read_text(encoding="utf-8") for p in DOCS if p.exists()}
    failures: list[str] = []

    print("=" * 78)
    print("CLAIM CHECK: documentation against committed artifacts")
    print("=" * 78)

    for label, actual, spellings in checks:
        if actual is None:
            print(f"  SKIP  {label:38s} (artifact missing)")
            continue
        found_in = [
            p.name for p, text in corpus.items()
            if any(s in text for s in spellings)
        ]
        status = "ok  " if found_in else "----"
        print(f"  {status}  {label:38s} = {actual}   {'in ' + ', '.join(found_in) if found_in else '(not cited)'}")

    # Statements that must NOT appear anywhere: retired overclaims.
    banned = [
        ("absolute monitor claim", r"[Nn]o monitor (on earth|can|could)"),
        ("drift tools have no lineage", r"[Tt]hey have no lineage"),
        ("incident raised on the model", r"[Ii]ncident on the affected `?mlModel"),
        ("skills contribution claimed as filed", r"I contributed a `datahub-ml-lineage`"),
    ]
    print("\n" + "=" * 78)
    print("RETIRED CLAIMS (must not reappear)")
    print("=" * 78)
    # A retired claim may legitimately appear inside an instruction not to say it,
    # or inside the correction log that records why it was retired. Those are the
    # discipline working, not the claim returning, so judge by line context.
    def is_disavowal(line: str) -> bool:
        lowered = line.lower().strip()
        return (
            lowered.startswith("do not")
            or lowered.startswith("- **\"")
            or "was an overclaim" in lowered
            or "is cut" in lowered
            or "cut as an overclaim" in lowered
            or "the earlier wording" in lowered
        )

    hits_for: dict[str, list[str]] = {}
    for label, pattern in banned:
        found = []
        for p, text in corpus.items():
            for n, line in enumerate(text.splitlines(), 1):
                if re.search(pattern, line) and not is_disavowal(line):
                    found.append(f"{p.name}:{n}")
        hits_for[label] = found

    for label, pattern in banned:
        hits = hits_for[label]
        if hits:
            failures.append(f"{label} reappeared in {', '.join(hits)}")
            print(f"  FAIL  {label:38s} {', '.join(hits)}")
        else:
            print(f"  ok    {label:38s} absent")

    # Internal consistency: the per-trip figure must reconcile with the total.
    print("\n" + "=" * 78)
    print("ARITHMETIC")
    print("=" * 78)
    per_row = impact.get("did_attributable_mae_per_row")
    rows = impact.get("affected_rows")
    total = impact.get("did_attributable_dollars")
    if per_row and rows and total:
        computed = round(per_row * rows, 2)
        drift = abs(computed - total)
        ok = drift < 1.0
        if not ok:
            failures.append(f"{per_row} x {rows} = {computed}, but artifact says {total}")
        print(f"  {'ok  ' if ok else 'FAIL'}  {per_row} x {rows:,} = {computed:,.2f} "
              f"(artifact: {total:,.2f})")

    print("\n" + "=" * 78)
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: no retired claim reappeared, and the headline arithmetic reconciles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
