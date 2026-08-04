"""The Culprit agent.

The model is the engine here. It decides which features look wrong, which
columns to walk back to, what the change in those columns means, and whether a
hypothesis survives contact with the evidence. Nothing about the NYC taxi feed,
vendor codes or one-hot encoding is written into this file. The agent is given
a model URN and a set of tools, and it works the problem.

The deterministic layer underneath (culprit/warehouse.py) exists so the agent
cannot invent a number. Every dollar and every row count in the final report is
returned by SQL. That is a guardrail on the engine, not a replacement for it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from culprit import datahub_graph as dg
from culprit import warehouse as wh
from culprit.llm import LLMClient, ToolResult, build_client
from culprit.mcp_bridge import DataHubMCP

MAX_TURNS = 40

# Tool results are resent every turn, so oversized ones dominate token use and
# push low-tier accounts over their per-minute limit. 8k characters is ample for
# the JSON these tools return while keeping a full investigation affordable.
MAX_TOOL_RESULT_CHARS = int(os.environ.get("CULPRIT_MAX_TOOL_RESULT_CHARS", "8000"))

SYSTEM = """\
You are Culprit, a diagnostic agent for silent machine-learning model decay.

A production model is producing degraded predictions. Conventional monitoring is
green: freshness, row volume, null rates and schema checks all pass. Your job is
to find the upstream cause and prove it.

The failure class you are looking for is SEMANTIC, not structural. A column
keeps its name, its type, its null rate and its row count, but its MEANING
changes. Examples of the shape (not necessarily this case): a categorical column
starts emitting a value that downstream encoding logic was never taught, a unit
silently changes, a backfill rewrites history, an upstream join changes
cardinality. Structural monitors are blind to all of these.

Method:

1. Read the model's context from the DataHub graph. Pay attention to what the
   training data actually contained versus what the model is being asked to
   score now.
2. Look at how each model input behaves across segments of the serving data.
   A single upstream change frequently damages a model by MORE THAN ONE route
   at once, and reporting only the first route you find understates the blast
   radius and produces an incomplete fix.

   Treat every signal the drift report raises as a separate finding to account
   for. Two independent things it can tell you about the same segment:
     - inputs degenerate only in that segment, meaning a derivation collapsed
     - every category indicator sitting at zero, meaning those rows match no
       category the transformation knows about
   These have different causes and different fixes. If both fire on the same
   segment, both belong in affected_features and both belong in the mechanism.
   Before you report, check your affected_features list against every signal
   that fired and confirm nothing is missing.
3. Walk the lineage backwards from a suspicious feature to the raw source
   columns it derives from. Use the graph, not guesswork.

   Several source columns will often look anomalous at once. Rank them by
   causal precedence rather than picking whichever you profiled first:

   - If the damage is confined to one segment, the column that DEFINES that
     segment is upstream of everything else anomalous within it. Other columns
     look strange *for those rows* precisely because of which rows they are.
     The defining column is the root cause; the rest are its symptoms.
   - Prefer the column whose change is a genuine change in the incoming data
     over a column that merely computes differently for the affected rows.
   - Sanity check: the root cause should explain every other anomaly you found.
     If it explains only some, you have picked a symptom.
4. Profile those source columns over time. You are looking for a change in the
   set of values, not a change in volume or shape.
5. Form a hypothesis that explains the mechanism end to end: source change ->
   transformation behaviour -> feature corruption -> model error. Test it.
6. Check what conventional monitoring would have seen, and report it honestly.
   Some metrics may well fire, and that does NOT mean you have the wrong root
   cause. What matters is whether the signal was actionable when the damage
   started: how large it was in that month, whether it stood clear of its own
   baseline noise, and above all whether it identified the affected model, the
   retrain that baked the change in, or the cost. Structural monitors watch the
   shape of a column. They do not attribute. State plainly which checks stayed
   silent, which fired, in which month, and how big the signal was at the point
   the damage began.
7. Quantify the damage using the counterfactual measurement tool, which nets out
   error that is intrinsic to the segment rather than caused by the defect.

Rules:

- Never state a number you did not receive from a tool. If you need a figure,
  call the tool that computes it.
- Distinguish what you have proven from what you infer. Say which is which.
- If the evidence does not support a confident root cause, report that instead
  of manufacturing one.
- When you are done, call report_root_cause exactly once.
"""


@dataclass
class TraceStep:
    """One tool call, recorded so the run can be replayed and audited."""

    index: int
    tool: str
    arguments: dict[str, Any]
    elapsed_ms: int
    result_preview: str


@dataclass
class Investigation:
    model_urn: str
    root_cause: dict[str, Any] | None = None
    trace: list[TraceStep] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    turns: int = 0
    stopped_reason: str = "completed"
    llm_model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None
    # Observed during the run, never hardcoded. The lineage diagram is drawn
    # from what the agent actually traversed, so it cannot show a path the
    # investigation did not take.
    lineage_urns: list[str] = field(default_factory=list)
    model_name: str | None = None
    training_window: str | None = None
    # Working store for hop deduplication and ordering; not serialised.
    _hop_depths: list[dict[str, Any]] = field(default_factory=list, repr=False)


# --------------------------------------------------------------------------
# Culprit's own tools, layered on top of what DataHub's MCP server provides
# --------------------------------------------------------------------------

LOCAL_TOOLS: dict[str, Callable[..., Any]] = {
    "get_model_context": dg.get_model_context,
    "get_feature_context": dg.get_feature_context,
    "get_upstream_lineage": dg.get_upstream_lineage,
    "find_production_models": dg.find_production_models,
    "list_columns": wh.list_columns,
    "profile_column_over_time": wh.profile_column_over_time,
    "feature_drift_report": wh.feature_drift_report,
    "measure_attributable_error": wh.measure_attributable_error,
    "check_standard_monitors": wh.check_standard_monitors,
}

LOCAL_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "get_model_context",
        "description": (
            "Fetch a production model from the DataHub graph: its inputs, version, "
            "algorithm, training metrics and the custom properties recorded at "
            "training time. Start here."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"model_urn": {"type": "string"}},
            "required": ["model_urn"],
        },
    },
    {
        "name": "get_feature_context",
        "description": (
            "Fetch one model input from the graph, including the dataset column it "
            "is derived from and the raw source columns behind it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"feature_urn": {"type": "string"}},
            "required": ["feature_urn"],
        },
    },
    {
        "name": "get_upstream_lineage",
        "description": (
            "Walk dataset lineage upstream from a dataset URN. This lineage was "
            "produced by dbt build artifacts, not asserted by hand."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset_urn": {"type": "string"},
                "depth": {"type": "integer", "default": 3},
            },
            "required": ["dataset_urn"],
        },
    },
    {
        "name": "find_production_models",
        "description": "List every mlModel in the DataHub graph.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_columns",
        "description": "List columns and types for a dataset URN or table name.",
        "input_schema": {
            "type": "object",
            "properties": {"dataset": {"type": "string"}},
            "required": ["dataset"],
        },
    },
    {
        "name": "profile_column_over_time",
        "description": (
            "Profile a column month by month: distinct value count, row count, null "
            "percentage, and for low-cardinality columns the full value set per "
            "month plus any values that appear part-way through the history. This "
            "is how a semantic change becomes visible."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string"},
                "column": {"type": "string"},
            },
            "required": ["dataset", "column"],
        },
    },
    {
        "name": "feature_drift_report",
        "description": (
            "For each segment of the serving data, report how the model's input "
            "features behave and what the model's error is. Raw statistics only; "
            "deciding what counts as a defect is your job."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"segment_column": {"type": "string", "default": "vendor_id"}},
        },
    },
    {
        "name": "measure_attributable_error",
        "description": (
            "Measure the dollar impact attributable to the defect for one segment, "
            "net of a counterfactual control model that does not have the defect. "
            "Use this for every monetary figure. Never estimate money yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "segment_column": {"type": "string", "default": "vendor_id"},
                "segment_value": {"type": ["integer", "string"]},
            },
            "required": ["segment_value"],
        },
    },
    {
        "name": "check_standard_monitors",
        "description": (
            "Evaluate the checks a conventional data-observability stack would run "
            "on a column (freshness, volume, null rate, schema) and report whether "
            "each would have fired."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string"},
                "column": {"type": "string"},
            },
            "required": ["dataset", "column"],
        },
    },
    {
        "name": "report_root_cause",
        "description": (
            "Report the final diagnosis. Call exactly once, when the evidence "
            "supports a conclusion or when you have established that it does not."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "confident": {
                    "type": "boolean",
                    "description": "False if the evidence does not support a confident diagnosis.",
                },
                "headline": {
                    "type": "string",
                    "description": "One sentence a data engineer could act on.",
                },
                "root_cause_dataset": {"type": "string"},
                "root_cause_column": {"type": "string"},
                "change_description": {
                    "type": "string",
                    "description": "What changed in the source data, and when.",
                },
                "mechanism": {
                    "type": "string",
                    "description": "Source change -> transformation -> feature -> model error.",
                },
                "affected_features": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Model inputs corrupted by the change.",
                },
                "why_monitors_missed_it": {"type": "string"},
                "impact": {
                    "type": "object",
                    "description": "Copy figures verbatim from measure_attributable_error.",
                },
                "recommended_fix": {
                    "type": "string",
                    "description": "The concrete change to make, naming the file or model.",
                },
                "proven": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Claims directly supported by tool output.",
                },
                "inferred": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Claims that are reasoned rather than measured.",
                },
            },
            "required": [
                "confident", "headline", "root_cause_dataset", "root_cause_column",
                "change_description", "mechanism", "affected_features",
                "why_monitors_missed_it", "impact", "recommended_fix", "proven", "inferred",
            ],
        },
    },
]

# DataHub MCP tools worth exposing to the agent. The mutation tools are excluded
# from the investigation loop on purpose: diagnosis reads, write-back is a
# separate, explicit step.
MCP_TOOLS_ALLOWED = {
    "search",
    "get_lineage",
    "get_entities",
    "list_schema_fields",
    "get_dataset_queries",
    "get_lineage_paths_between",
}


def _preview(text: str, limit: int = 400) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + " ..."


def _observe(inv: "Investigation", tool: str, result: Any) -> None:
    """Record graph facts the agent surfaced, for the trace diagram.

    Everything here comes from tool output during this run. Nothing about the
    dataset under investigation is assumed, so the rendered lineage path can
    only ever show hops the investigation actually visited.
    """
    if not isinstance(result, (dict, list)):
        return

    if tool == "get_model_context" and isinstance(result, dict):
        inv.model_name = result.get("name") or inv.model_name
        version = result.get("version")
        if version and inv.model_name and version not in inv.model_name:
            inv.model_name = f"{inv.model_name} v{version}"
        props = result.get("custom_properties") or {}
        window = props.get("training_months") or props.get("vendors_in_training_data")
        if props.get("training_months"):
            inv.training_window = props["training_months"]
        elif window:
            inv.training_window = str(window)

    elif tool == "get_feature_context" and isinstance(result, dict):
        for urn in result.get("source_datasets") or []:
            _add_hop(inv, urn, depth=0)

    elif tool == "get_upstream_lineage" and isinstance(result, list):
        for hop in result:
            if isinstance(hop, dict) and hop.get("dataset"):
                _add_hop(inv, hop["dataset"], depth=int(hop.get("hop", 0)))


def _table_key(urn: str) -> str:
    """A platform-independent identity for a dataset.

    DataHub's dbt connector creates two datasets per model, one for the dbt node
    and one for the target-platform table. They are the same table and should
    appear once in a lineage diagram. Comparing the trailing schema.table
    segments collapses the pair without needing to know either platform.
    """
    try:
        name = urn.split(",")[1]
    except IndexError:
        return urn
    return ".".join(name.split(".")[-2:])


def _add_hop(inv: "Investigation", urn: str, depth: int) -> None:
    """Record a lineage hop, keeping one entry per real table.

    Hops are stored with their distance from the starting point so the diagram
    can be drawn in traversal order rather than in the order tools happened to
    be called.
    """
    key = _table_key(urn)
    for existing in inv._hop_depths:  # noqa: SLF001 - same class
        if existing["key"] == key:
            existing["depth"] = max(existing["depth"], depth)
            return
    inv._hop_depths.append({"key": key, "urn": urn, "depth": depth})

    # Deepest upstream first: that is the direction the story reads, from the
    # column that changed down to the model that absorbed it.
    inv._hop_depths.sort(key=lambda h: -h["depth"])
    inv.lineage_urns = [h["urn"] for h in inv._hop_depths]


def investigate(
    model_urn: str,
    symptom: str,
    mcp: DataHubMCP | None = None,
    verbose: bool = True,
    client: LLMClient | None = None,
) -> Investigation:
    """Run one investigation to a root cause."""
    client = client or build_client()

    tools = list(LOCAL_TOOL_SPECS)
    mcp_specs: dict[str, dict[str, Any]] = {}
    if mcp is not None:
        for spec in mcp.tools:
            if spec["name"] in MCP_TOOLS_ALLOWED:
                mcp_specs[spec["name"]] = spec
                tools.append(
                    {
                        "name": f"datahub_{spec['name']}",
                        "description": "[DataHub MCP server] " + spec["description"][:900],
                        "input_schema": spec["input_schema"],
                    }
                )

    investigation = Investigation(model_urn=model_urn, llm_model=client.model)

    client.start(SYSTEM, tools)
    client.send_user(
        f"Production model under investigation:\n  {model_urn}\n\n"
        f"Reported symptom:\n  {symptom}\n\n"
        "Diagnose it."
    )

    started = time.perf_counter()
    step = 0
    nudged = False

    for turn in range(MAX_TURNS):
        investigation.turns = turn + 1
        response = client.step()

        if verbose:
            for text in response.text:
                print(f"\n  [thinking] {_preview(text, 300)}")

        if not response.tool_calls:
            # A model that has finished reasoning will often write its
            # conclusion as prose instead of calling the reporting tool. That is
            # a formatting slip, not a failed investigation, so ask once before
            # giving up rather than discarding the work.
            if not nudged:
                nudged = True
                if verbose:
                    print("\n  [nudge] asking the model to file its finding as a tool call")
                client.send_user(
                    "You stopped without calling report_root_cause. If you have "
                    "reached a conclusion, call report_root_cause now with your "
                    "findings, copying any figures verbatim from the tool output "
                    "you already received. If the evidence does not support a "
                    "confident diagnosis, call it with confident=false and say why."
                )
                continue
            investigation.stopped_reason = "model stopped without reporting"
            break

        results: list[ToolResult] = []
        finished = False

        for call in response.tool_calls:
            step += 1
            name, args = call.name, call.arguments
            call_started = time.perf_counter()

            if name == "report_root_cause":
                investigation.root_cause = args
                finished = True
                payload = "Root cause recorded."
            elif name in LOCAL_TOOLS:
                try:
                    raw = LOCAL_TOOLS[name](**args)
                    _observe(investigation, name, raw)
                    payload = json.dumps(raw, default=str)
                except Exception as exc:  # noqa: BLE001 - surfaced back to the agent
                    payload = f"ERROR: {type(exc).__name__}: {exc}"
            elif name.startswith("datahub_") and mcp is not None:
                try:
                    payload = mcp.call(name[len("datahub_"):], args)
                except Exception as exc:  # noqa: BLE001
                    payload = f"ERROR: {type(exc).__name__}: {exc}"
            else:
                payload = f"ERROR: unknown tool {name}"

            elapsed_ms = int((time.perf_counter() - call_started) * 1000)
            investigation.trace.append(
                TraceStep(step, name, args, elapsed_ms, _preview(str(payload)))
            )
            if verbose:
                detail = ", ".join(f"{k}={v}" for k, v in list(args.items())[:2])
                print(f"  [{step:2d}] {name}({_preview(detail, 90)})  {elapsed_ms}ms")

            results.append(
                ToolResult(id=call.id, content=str(payload)[:MAX_TOOL_RESULT_CHARS])
            )

        # Tool results must go back even on the final turn: several providers
        # reject a conversation with an unanswered tool call.
        client.send_tool_results(results)
        if finished:
            break
    else:
        investigation.stopped_reason = "turn limit reached"

    investigation.elapsed_seconds = round(time.perf_counter() - started, 2)
    investigation.input_tokens = client.input_tokens
    investigation.output_tokens = client.output_tokens
    investigation.estimated_cost_usd = client.estimated_cost_usd()
    return investigation
