"""Generate, prove, and propose the fix.

Finding a root cause and filing an incident leaves the actual repair to a human
who now has to re-derive what the agent already worked out. This module closes
that loop: it locates the transformation at fault, has the model write a patch
grounded in the real file, and then **proves the patch works by running dbt
against the real warehouse** before anything is proposed.

The proof is the point. A generated diff that merely looks plausible is worth
very little. Three gates have to pass before a PR is opened:

  1. dbt parses and builds the patched model
  2. the rebuilt table no longer leaves the affected rows unmatched
  3. no other segment's row count changed

Failing any gate means no PR. Culprit reports the failure instead, because
proposing an unverified change to production transformation code is worse than
proposing nothing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from culprit.llm import LLMClient

ROOT = Path(__file__).resolve().parents[1]
DBT_DIR = ROOT / "pipeline" / "dbt"
MODELS_DIR = DBT_DIR / "models"
WAREHOUSE = ROOT / "pipeline" / "warehouse.duckdb"


@dataclass
class Remediation:
    transformation_path: Path | None = None
    original_sql: str = ""
    patched_sql: str = ""
    diff: str = ""
    validated: bool = False
    gates: dict[str, Any] = field(default_factory=dict)
    branch: str | None = None
    pull_request_url: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# 1. Locate the transformation at fault
# ---------------------------------------------------------------------------

def find_transformation(
    affected_features: list[str], root_cause_column: str, models_dir: Path = MODELS_DIR
) -> Path | None:
    """Find the model file that both reads the offending column and defines the
    damaged outputs. Scored rather than guessed, so it generalises past this repo.
    """
    best: tuple[int, Path] | None = None
    for path in sorted(models_dir.rglob("*.sql")):
        text = path.read_text(encoding="utf-8")
        score = sum(1 for f in affected_features if re.search(rf"\b{re.escape(f)}\b", text))
        if re.search(rf"\b{re.escape(root_cause_column)}\b", text):
            score += 1
        if score and (best is None or score > best[0]):
            best = (score, path)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# 2. Have the model write the patch, grounded in the real file
# ---------------------------------------------------------------------------

PATCH_SYSTEM = """\
You repair dbt transformation code.

You are given the exact current contents of a dbt model and a diagnosed defect
in it. Return the complete corrected file and nothing else.

Rules:
- Output raw SQL only. No markdown fence, no commentary, no explanation.
- Change as little as possible. Preserve every existing column, its name, its
  order and its formatting. A reviewer should see a small diff.
- Preserve the existing comments, and add a brief one explaining the new logic.
- Do not invent column names that do not exist in the source data.

Hard constraints. A patch violating any of these is rejected outright:

- **Never drop or filter rows.** Do not add or narrow a WHERE clause, do not add
  a JOIN that could exclude rows, do not add LIMIT or QUALIFY. The output must
  contain exactly the same rows as before. Excluding the affected rows makes the
  symptom disappear while destroying the data, which is far worse than the bug.
- **Fix the representation, not the population.** The affected rows must remain
  and must end up correctly encoded.
- Prefer logic that will not silently break again the next time a new value
  appears upstream, and note that in a comment.
"""


def propose_patch(
    client: LLMClient,
    sql: str,
    root_cause: dict[str, Any],
    unmapped_values: list[str],
    evidence: str,
) -> str:
    """Ask the model for a corrected file. Returns raw SQL."""
    prompt = f"""\
Current contents of `{root_cause.get('transformation_name', 'the dbt model')}`:

```sql
{sql}
```

Diagnosed defect
----------------
Root cause column : {root_cause.get('root_cause_column')}
Unmapped value(s) : {', '.join(unmapped_values) or 'unknown'}
Damaged outputs   : {', '.join(root_cause.get('affected_features') or [])}

What changed upstream: {root_cause.get('change_description')}

Mechanism: {root_cause.get('mechanism')}

Measured evidence:
{evidence}

Return the complete corrected file.
"""
    client.start(PATCH_SYSTEM, [])
    client.send_user(prompt)
    reply = client.step()
    out = "\n".join(reply.text).strip()
    # Models add a fence despite being told not to; strip it rather than fail.
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z]*\n", "", out)
        out = re.sub(r"\n```\s*$", "", out)
    return out.strip() + "\n"


def unified_diff(original: str, patched: str, path: Path) -> str:
    import difflib

    rel = path.relative_to(ROOT).as_posix()
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )


# ---------------------------------------------------------------------------
# 3. Prove it. This is the part that matters.
# ---------------------------------------------------------------------------

def _dbt_executable() -> str:
    candidate = Path(sys.executable).parent / ("dbt.exe" if os.name == "nt" else "dbt")
    return str(candidate) if candidate.exists() else (shutil.which("dbt") or "dbt")


def _segment_unmatched(segment_column: str, segment_value: Any, table: str) -> dict[str, Any]:
    """Do the affected rows still match no category indicator?

    Only indicators belonging to the *segment column's own encoding family*
    count. A column is in that family when it is binary AND functionally
    determined by the segment column, meaning it is constant within every
    segment. That test is what separates `is_vendor_*`, which encodes the
    segment, from unrelated flags like a payment-type indicator that happen to
    be binary but vary freely inside a segment. Counting the latter made this
    gate unfireable.
    """
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {table}").fetchall()]
        indicators = []
        for c in cols:
            if c == segment_column:
                continue
            vals = con.execute(
                f'SELECT DISTINCT TRY_CAST("{c}" AS DOUBLE) v FROM {table} WHERE "{c}" IS NOT NULL'
            ).df()["v"].dropna().tolist()
            if not vals or set(vals) - {0.0, 1.0}:
                continue
            # Constant within every segment?
            varies = con.execute(
                f'SELECT MAX(sd) FROM (SELECT STDDEV_POP(TRY_CAST("{c}" AS DOUBLE)) sd '
                f'FROM {table} GROUP BY "{segment_column}")'
            ).fetchone()[0]
            if (varies or 0) == 0:
                indicators.append(c)
        if not indicators:
            return {"indicators": [], "all_zero": False}
        total = " + ".join(f'COALESCE(TRY_CAST("{c}" AS DOUBLE), 0)' for c in indicators)
        row = con.execute(
            f'SELECT AVG({total}) AS active, COUNT(*) AS rows FROM {table} '
            f'WHERE "{segment_column}" = ?',
            [segment_value],
        ).fetchone()
    return {
        "indicators": indicators,
        "mean_active_indicators": round(float(row[0] or 0), 4),
        "rows": int(row[1] or 0),
        "all_zero": float(row[0] or 0) == 0.0,
    }


def validate_patch(
    path: Path,
    patched_sql: str,
    segment_column: str,
    segment_value: Any,
    table: str = "main_marts.fct_trip_features",
) -> dict[str, Any]:
    """Apply the patch, run dbt for real, then check the defect is gone.

    The original file is always restored, whether or not the gates pass.
    """
    gates: dict[str, Any] = {}
    original = path.read_text(encoding="utf-8")

    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        before_counts = con.execute(
            f'SELECT "{segment_column}" s, COUNT(*) n FROM {table} GROUP BY 1'
        ).df().set_index("s")["n"].to_dict()
    before = _segment_unmatched(segment_column, segment_value, table)
    gates["before"] = before

    backup = tempfile.NamedTemporaryFile(delete=False, suffix=".sql")
    backup.write(original.encode("utf-8"))
    backup.close()

    try:
        path.write_text(patched_sql, encoding="utf-8")
        env = dict(os.environ, DBT_PROFILES_DIR=str(DBT_DIR))
        proc = subprocess.run(
            [_dbt_executable(), "build", "--select", path.stem],
            cwd=str(DBT_DIR), env=env, capture_output=True, text=True, timeout=1800,
        )
        gates["dbt_build_ok"] = proc.returncode == 0
        gates["dbt_output_tail"] = "\n".join(
            (proc.stdout or proc.stderr).strip().splitlines()[-12:]
        )
        if proc.returncode != 0:
            return gates

        after = _segment_unmatched(segment_column, segment_value, table)
        gates["after"] = after
        gates["defect_resolved"] = bool(before.get("all_zero") and not after.get("all_zero"))

        with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
            after_counts = con.execute(
                f'SELECT "{segment_column}" s, COUNT(*) n FROM {table} GROUP BY 1'
            ).df().set_index("s")["n"].to_dict()
        gates["row_counts_unchanged"] = before_counts == after_counts
        gates["row_counts"] = {"before": before_counts, "after": after_counts}

        gates["passed"] = bool(
            gates["dbt_build_ok"]
            and gates["defect_resolved"]
            and gates["row_counts_unchanged"]
        )
        return gates
    finally:
        # Restore. The PR carries the change; the working tree does not.
        path.write_text(Path(backup.name).read_text(encoding="utf-8"), encoding="utf-8")
        Path(backup.name).unlink(missing_ok=True)
        subprocess.run(
            [_dbt_executable(), "build", "--select", path.stem],
            cwd=str(DBT_DIR), env=dict(os.environ, DBT_PROFILES_DIR=str(DBT_DIR)),
            capture_output=True, text=True, timeout=1800,
        )


# ---------------------------------------------------------------------------
# 4. Propose it
# ---------------------------------------------------------------------------

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True, timeout=180
    )


def open_pull_request(
    path: Path, patched_sql: str, root_cause: dict[str, Any], gates: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Commit the patch on a branch and open a PR. Returns (branch, url)."""
    column = root_cause.get("root_cause_column", "column")
    branch = f"culprit/fix-{re.sub(r'[^a-z0-9]+', '-', column.lower())}"

    starting = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
    try:
        _git("branch", "-D", branch)
        created = _git("checkout", "-b", branch)
        if created.returncode != 0:
            return None, f"could not create branch: {created.stderr.strip()}"

        path.write_text(patched_sql, encoding="utf-8")
        _git("add", str(path.relative_to(ROOT).as_posix()))

        after = gates.get("after", {})
        body = f"""\
Fixes the transformation defect diagnosed by Culprit.

**Root cause:** `{root_cause.get('root_cause_dataset')}.{column}`

{root_cause.get('change_description', '')}

**Mechanism:** {root_cause.get('mechanism', '')}

### Verification

This patch was not just generated, it was executed. Before opening this PR
Culprit applied it, ran `dbt build` against the real warehouse, and checked the
defect was actually gone:

| gate | result |
|---|---|
| `dbt build` succeeds | {'PASS' if gates.get('dbt_build_ok') else 'FAIL'} |
| affected rows now match a category | {'PASS' if gates.get('defect_resolved') else 'FAIL'} |
| no other segment's row count changed | {'PASS' if gates.get('row_counts_unchanged') else 'FAIL'} |

Mean active category indicators for the affected segment went from
**{gates.get('before', {}).get('mean_active_indicators')}** to
**{after.get('mean_active_indicators')}**.

### Note for the reviewer

Correcting the transformation stops new rows being mis-encoded. It does **not**
repair the deployed model, which was trained before this value existed and has
never seen it. A retrain is required for the measured error to actually go away.
"""
        commit = _git("commit", "-m", f"Fix encoding for unmapped values in {path.stem}\n\n{body}")
        if commit.returncode != 0:
            return branch, f"commit failed: {commit.stderr.strip()}"

        pushed = _git("push", "-u", "origin", branch, "--force")
        if pushed.returncode != 0:
            return branch, f"push failed: {pushed.stderr.strip()}"

        pr = subprocess.run(
            ["gh", "pr", "create", "--title",
             f"Fix encoding for unmapped values in {column}", "--body", body,
             "--base", starting, "--head", branch],
            cwd=str(ROOT), capture_output=True, text=True, timeout=180,
        )
        url = pr.stdout.strip().splitlines()[-1] if pr.returncode == 0 else None
        return branch, url or f"gh pr create failed: {pr.stderr.strip()}"
    finally:
        _git("checkout", "--", str(path.relative_to(ROOT).as_posix()))
        _git("checkout", starting)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def remediate(
    client: LLMClient,
    root_cause: dict[str, Any],
    unmapped_values: list[str],
    evidence: str,
    segment_column: str,
    segment_value: Any,
    create_pr: bool = False,
) -> Remediation:
    out = Remediation()
    path = find_transformation(
        root_cause.get("affected_features") or [],
        root_cause.get("root_cause_column") or "",
    )
    if path is None:
        out.error = "could not locate the transformation responsible"
        return out

    out.transformation_path = path
    out.original_sql = path.read_text(encoding="utf-8")
    root_cause = {**root_cause, "transformation_name": path.name}
    out.patched_sql = propose_patch(
        client, out.original_sql, root_cause, unmapped_values, evidence
    )
    if out.patched_sql.strip() == out.original_sql.strip():
        out.error = "the model returned the file unchanged"
        return out

    out.diff = unified_diff(out.original_sql, out.patched_sql, path)
    out.gates = validate_patch(path, out.patched_sql, segment_column, segment_value)
    out.validated = bool(out.gates.get("passed"))

    if out.validated and create_pr:
        out.branch, out.pull_request_url = open_pull_request(
            path, out.patched_sql, root_cause, out.gates
        )
    return out
