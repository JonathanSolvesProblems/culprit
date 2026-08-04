# Culprit

**A stack trace for model decay.**

Freshness, volume, null-rate and schema checks were all green. The model had been
quietly wrong for six months. Culprit walked DataHub's ML lineage back to the
column that did it and filed the incident into the graph with the dollars
attached.

Built for [Build with DataHub: The Agent Hackathon](https://datahub.devpost.com).
Challenge: **Production ML Agents**. Apache 2.0.

---

## The number

> **$90,322 of attributable model error in a single month, across 66,146 real NYC
> taxi trips, caused by one upstream column's maximum value changing from 6 to 7,
> while freshness, volume, null-rate and schema checks all stayed green.**

Measured, not estimated: computed in SQL against 19.3M real records, net of a
counterfactual control model, using a difference-in-differences estimator that
subtracts the control's unearned data advantage ($0.0731 per row, measured on
3,840,878 unaffected rows). The naive control difference is $95,158; the stricter
figure is the one quoted. Methodology, including the ways it could be wrong, in
[Measuring the damage](#measuring-the-damage).

Per trip, that is **$1.37**.

## The problem

Drift tools tell you **what** moved. They cannot tell you **why**, because they
have no lineage. Lineage tools show you the path, but have no idea anything is
wrong. Neither one fires at all for the failure class that costs the most:

**A column keeps its name, its type, its null rate and its row count, but changes
its meaning.**

No structural monitor can see that. There is nothing to see. The pipeline is
green, the schema is stable, the row counts are normal, and the model is quietly
wrong for months.

## The real incident this is built on

The fault in this repository is **not planted**. It genuinely happened, in a
public dataset, and you can verify it yourself with
[`scripts/scan_tlc_semantics.py`](scripts/scan_tlc_semantics.py).

A new taxi vendor entered the NYC Taxi and Limousine Commission feed:

| feed month | `VendorID = 7` trips | share of feed |
|---|---:|---:|
| 2024-06 | 0 | 0% |
| 2024-09 | 0 | 0% |
| **2024-12** | **230** | **0.006%** |
| 2025-03 | 21,481 | 0.5% |
| 2025-06 | 67,573 | 1.6% |

It arrives at a volume so small that no threshold could catch it, then compounds
by nearly 300x over six months.

The `fct_trip_features` dbt model one-hot encodes vendors with a hardcoded `CASE`
over the three vendors that existed when it was written. That is ordinary,
defensible dbt code. It is also the culprit:

```sql
case when vendor_id = 1 then 1 else 0 end as is_vendor_cmt,
case when vendor_id = 2 then 1 else 0 end as is_vendor_curb,
case when vendor_id = 6 then 1 else 0 end as is_vendor_myle,
```

A vendor-7 trip asserts "no vendor", a combination that appears nowhere in the
training data.

There is a second defect stacked on top, and I did not plant that either. Vendor
7 emits identical pickup and dropoff timestamps, so every one of its trips has a
duration of exactly zero. The feature model guards against division by zero the
way everyone does:

```sql
coalesce(trip_distance / nullif(trip_minutes / 60.0, 0), 0) as avg_speed_mph
```

**The null-safety guard is what hides the corruption.** Without it, `avg_speed_mph`
would go NULL and a null-rate monitor would fire. With it, the column stays clean
and confidently reports 0 mph for 66,146 trips.

Here is what the structural checks saw:

| feed month | row volume | null % | dtype | min | max |
|---|---:|---:|---|---:|---:|
| 2024-06 | 3,539,193 | 0.0 | INTEGER | 1 | 6 |
| 2024-09 | 3,633,030 | 0.0 | INTEGER | 1 | 6 |
| 2024-12 | 3,668,371 | 0.0 | INTEGER | 1 | **7** |
| 2025-03 | 4,145,257 | 0.0 | INTEGER | 1 | 7 |
| 2025-06 | 4,322,960 | 0.0 | INTEGER | 1 | 7 |

One integer changed. That is the whole signal at the source.

### The obvious objection, named first

DataHub Cloud's anomaly detection covers five column metrics, not four checks, and
its Column Value assertions have In Set / Not In Set operators. So: **would
anything have caught this?** I ran the full sweep rather than only the metrics
that flatter the story, at both the source and the derived-feature layer. The raw
output is in [examples/02_monitor_sweep.json](examples/02_monitor_sweep.json).

Two metrics do fire.

`unique_count` on `vendor_id` goes 3 to 4 in 2024-12. That is the same integer the
max value already shows, and it names no model.

`zero_count` on `avg_speed_mph` climbs, precisely because the `coalesce` that
dodges the null check lands the corruption in the zero count instead:

| feed month | zero rows | share of table |
|---|---:|---:|
| 2024-06 | 43 | 0.0013% |
| 2024-09 | 33 | 0.0010% |
| **2024-12** | **255** | **0.0073%** |
| 2025-03 | 21,350 | 0.5580% |
| 2025-06 | 66,174 | 1.6937% |

In 2024-12, the month the defect entered production, that is 255 rows out of
3,502,209, against a baseline already swinging between 33 and 43. It is
indistinguishable from noise. The first month it is unmissable is 2025-03, a full
quarter after the model started serving the new vendor wrong.

And an In Set assertion on `vendor_id` declaring {1, 2, 6} genuinely would have
failed in 2024-12. Two things about that: nobody declares an allowed-value set on
a three-value integer ID column, and if they had, it would have failed
`raw.yellow_trips` with no indication that a fare model three hops downstream was
absorbing it, which retrain baked it in, or what it cost.

That is the actual argument. When these signals fire they say *some speeds are
zero*. They do not name `nyc_fare_predictor`, do not name the training run, and do
not produce $90,322. **Detection was never the hard part. Attribution is.**
Culprit starts where those assertions stop.

## What Culprit does

Culprit is handed a model URN and a vague human symptom ("quotes have drifted
upward, nobody knows why"). It then:

1. Reads the model's context from the DataHub graph, including what its training
   data actually contained.
2. Compares how each model input behaves across segments of live serving data,
   looking for inputs that collapse to a constant or take impossible values.
3. Walks lineage backwards from the suspicious feature, through the ML entities
   and into the dbt-derived column lineage, to the raw source column.
4. Profiles that column over time to find the change in **meaning**.
5. Confirms that standard monitors would not have fired.
6. Quantifies the damage in dollars, in SQL, net of a control.
7. Writes the finding back into DataHub.

Nothing about taxis, vendors, or one-hot encoding appears in
[`culprit/agent.py`](culprit/agent.py). The agent is given tools and a method,
and it works the problem.

## The recorded run

Not a description of what it should do. This is the run committed in
[examples/investigation.json](examples/investigation.json), reproducible with
`culprit replay`:

| | |
|---|---|
| model | `gpt-4o` |
| tool calls | 13 |
| turns | 6 |
| wall clock | **36.51 seconds** |
| tokens | 29,739 in / 1,900 out |
| cost | **$0.093** |
| verdict | confident |
| root cause | `raw.yellow_trips.vendor_id` |
| affected inputs | `is_vendor_cmt`, `is_vendor_curb`, `is_vendor_myle` |

The agent was given a model URN and one vague sentence ("upfront fare quotes have
drifted upward, nobody knows why"). It read the model's context, compared feature
behaviour across segments, pulled five features' lineage, profiled four columns
over time, measured the impact, and filed the finding. Then it wrote an incident,
a document and a column annotation back into DataHub, all of which are live on
the instance.

**Honest note on run-to-run variation.** The upstream change damages the model by
two separate routes: the unmapped vendor encoding, and the collapse of
`trip_minutes` / `avg_speed_mph` caused by that vendor's degenerate timestamps.
Across runs the agent reliably identifies the root cause column and the December
2024 date, but it has reported one route or the other rather than both. The
recorded run found the encoding route. An earlier run found the timestamp route
and is described in [docs/DATAHUB_FINDINGS.md](docs/DATAHUB_FINDINGS.md). Getting
both reported in a single pass consistently is unfinished work, and it is listed
in [Limitations](#limitations) rather than papered over.

## The trace it produces

```
raw.yellow_trips.vendor_id            <- root cause, new value 7 from 2024-12
  |
  +-- stg_yellow_trips                (dbt, column-level lineage)
       |
       +-- fct_trip_features          (dbt, column-level lineage)
            |  is_vendor_cmt / is_vendor_curb / is_vendor_myle  -> all 0
            |  trip_minutes -> 0      avg_speed_mph -> 0 (coalesced)
            |
            +-- mlFeature x13         (DataHub ML entities)
                 |
                 +-- mlFeatureTable: nyc_fare_features
                      |
                      +-- mlModel: nyc_fare_predictor v1.4.0
                           ^
                           +-- dataProcessInstance: training run
                               vendors_in_training_data = [1, 2, 6]
```

That last line is the proof. The graph knows the model was trained on vendors 1,
2 and 6. The warehouse knows it is being asked to score vendor 7. No other system
holds both of those facts.

## How this uses DataHub

Culprit does not reimplement catalog access, and it does not only read.

**Reads through DataHub's own MCP server.** [`culprit/mcp_bridge.py`](culprit/mcp_bridge.py)
launches `mcp-server-datahub` over stdio and exposes its tools to the agent
(`search`, `get_lineage`, `get_entities`, `list_schema_fields`,
`get_dataset_queries`, `get_lineage_paths_between`).

**Contributes the ML half of the graph.** No DataHub sample datapack ships ML
entities, so [`pipeline/emit_ml_lineage.py`](pipeline/emit_ml_lineage.py) emits
13 `mlFeature`s, an `mlFeatureTable`, an `mlModelGroup`, an `mlModel` and a
`dataProcessInstance` training run through the DataHub Python SDK. Each feature
records the exact source column it derives from, which is what makes the walk
from model back to column possible.

**Uses real, ingested lineage rather than hand-asserted lineage.** The dataset
half of the graph comes from DataHub's native dbt connector parsing real
`manifest.json` and `catalog.json` build artifacts, which produces genuine
column-level `fineGrainedLineage`.

**Writes findings back.** [`culprit/writeback.py`](culprit/writeback.py) raises a
DataHub **Incident** on the affected `mlModel` via `raiseIncident`, saves the full
investigation as a knowledge **document** via the MCP `save_document` tool, and
annotates the offending source column so the next person or agent who opens it
inherits the finding instead of rediscovering it.

## Measuring the damage

The honest part. A dollar figure is easy to inflate, so here is exactly how this
one is produced and where it could be attacked.

Two models are trained with identical hyperparameters:

| | training window | rows | vendors seen | encoder |
|---|---|---:|---|---|
| **production** | 2024-06, 2024-09 | 6,883,008 | 1, 2, 6 | no slot for vendor 7 |
| **control** | 2024-06, 2024-09, 2025-03 | 10,709,506 | 1, 2, 6, 7 | has `is_vendor_helix` |

Both score the full real 2025-06 month (3,907,024 trips):

| vendor | trips | production MAE | control MAE | gap |
|---|---:|---:|---:|---:|
| 2 | 3,022,381 | $2.6364 | $2.5613 | $0.0751 |
| 1 | 817,102 | $2.8301 | $2.7669 | $0.0632 |
| **7** | **66,146** | **$4.6060** | **$3.1674** | **$1.4386** |
| 6 | 1,395 | $7.3657 | $5.9787 | $1.3870 |

**The obvious objection:** the control model saw more data, and fresher data. Some
of its advantage is just that, not the encoding fix.

That objection is correct, and it is why the naive gap is not the headline. The
control's unearned advantage is directly measurable on the segments that have no
encoding defect. Across 3,840,878 unaffected rows it is **$0.0731 per row**.
Subtracting it gives a difference-in-differences estimate:

```
did = (prod_mae_v7 - ctrl_mae_v7) - (prod_mae_baseline - ctrl_mae_baseline)
    = $1.4386 - $0.0731
    = $1.3655 per trip

$1.3655 x 66,146 trips = $90,322.36
```

Both estimators are returned by `measure_attributable_error`. The stricter one is
the headline.

**What this number is:** the model's prediction error on affected trips, in
dollars, that the fix actually recovers.

**What it is not:** realised revenue loss. Whether prediction error becomes real
money depends on whether the model drives upfront pricing. The honest exposure
figure is separate: **$1,698,233.95 of gross fare flowed through the affected
trips.**

**Vendor 6 is a real second finding, not noise.** It shows a $1.387 gap and an
average speed of 61.95 mph, which is not plausible for Manhattan taxi traffic. I
did not plant it and did not know about it before building. On 1,395 trips it is
worth roughly $1,900, so it does not change the headline, but it is left in
because suppressing an inconvenient real finding would be worse than reporting a
slightly messier result.

## Quickstart

Prerequisites: Docker, Python 3.11 or 3.12, and about 8 GB of RAM free for
DataHub. Python 3.13+ is not yet supported by the DataHub SDK.

**One command, start to finish:**

```bash
make demo                                    # macOS / Linux
powershell -ExecutionPolicy Bypass -File scripts\demo.ps1    # Windows
```

**In a hurry?** This needs no Docker, no DataHub and no API key, and it proves the
incident is real in about a minute:

```bash
make verify
```

The step-by-step version follows, if you would rather see each piece.

```bash
git clone <this repo> && cd BuildwithDataHub

python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt

# 1. DataHub OSS, local. Takes 5-15 minutes on first run (image pulls).
datahub docker quickstart
#    UI at http://localhost:9002   (datahub / datahub)

# 2. Build the real warehouse. Downloads ~250 MB of real NYC TLC parquet.
python pipeline/load_raw.py

# 3. Real dbt transforms, and the artifacts DataHub will ingest.
cd pipeline/dbt && dbt build && dbt docs generate && cd ../..

# 4. Ingest real column-level lineage through DataHub's own dbt connector.
cd pipeline && datahub ingest -c ingest_dbt.yml && cd ..

# 5. Train the production model and the counterfactual control.
python pipeline/train_model.py

# 6. Score the real 2025-06 month.
python pipeline/score_batch.py --month 2025-06

# 7. Emit the ML lineage that no sample datapack provides.
python pipeline/emit_ml_lineage.py

# 8. Run the investigation. Use whichever provider you already have.
export OPENAI_API_KEY=sk-...          # or ANTHROPIC_API_KEY=sk-ant-...
python -m culprit.cli investigate --write-back
```

Step 8 is the only step that needs a model. Steps 1 through 7 stand up the entire
environment and can be verified on their own.

**No API key at all?** Replay a recorded real investigation instead:

```bash
python -m culprit.cli replay --animate
```

### Choosing a model

Culprit's reasoning is provider-agnostic, matching the convention DataHub's own
Analytics Agent uses. It picks up whichever key is present, or you can be
explicit with `LLM_PROVIDER` and `LLM_MODEL`. See [.env.example](.env.example).

| provider | set | notes |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | defaults to `gpt-4o` |
| Anthropic | `ANTHROPIC_API_KEY` | defaults to `claude-sonnet-5` |
| Anything OpenAI-compatible | `OPENAI_BASE_URL` | Ollama, LiteLLM, vLLM. Free and local. |

A full investigation is roughly 15 to 30 tool calls. Every run prints its own
token usage and an estimated cost, so you can see exactly what it costs rather
than guessing.

A local model through Ollama costs nothing and the code path is identical. Be
aware that this is a genuinely hard multi-step reasoning task (notice the
anomaly, choose what to walk, form a hypothesis, test it), and smaller local
models are noticeably weaker at it. The recorded investigation in `examples/`
notes which model produced it.

To confirm the incident is real before trusting anything else here:

```bash
python scripts/scan_tlc_semantics.py     # finds VendorID=7 in the real TLC feed
python scripts/validate_impact.py        # shows vendor 7 is materially different
```

## What is real

Shipping a judged demo against a simulator is a mistake I have made before, so
to be explicit about every component:

| component | status |
|---|---|
| NYC TLC trip data | **Real.** 19.3M published records, true volumes, no sampling. |
| The semantic change | **Real.** Genuinely occurred in the public feed in Dec 2024. |
| The warehouse | **Real** DuckDB, real SQL. |
| The transforms | **Real** dbt (dbt-duckdb), real build artifacts. |
| Dataset lineage | **Real,** ingested by DataHub's dbt connector, not asserted. |
| ML lineage | **Real** DataHub ML entities via the Python SDK. |
| DataHub | **Real** OSS instance in Docker. Not mocked. |
| MCP server | **Real** `mcp-server-datahub` 0.6.0 over stdio, 19 tools. |
| The model | **Real** sklearn model trained on 6.88M rows. |
| The damage | **Real,** computed in SQL, net of a control. |
| Write-back | **Real** `raiseIncident` and `save_document` against the live instance. |

Nothing in this repository is simulated.

## Limitations

- The feature-to-column mapping is recorded by Culprit's own emitter as a custom
  property. In a production feature store that mapping would come from the store
  itself. The traversal logic does not change.
- Culprit currently detects semantic change in low-cardinality columns. Unit
  changes and backfill-driven leakage are described in the agent's method but
  only the new-categorical-value path is exercised end to end here.
- The counterfactual control requires being able to retrain. Where retraining is
  expensive, the naive estimator is the fallback and it overstates.
- The agent reports one of the two damage routes per run rather than both. It
  finds the root cause column and the date reliably; enumerating the full blast
  radius in a single pass is not yet dependable.
- Low-tier API accounts have small per-minute token allowances and an agent loop
  resends its context every turn. Culprit retries with backoff, but a very
  constrained account will still be slow.
- One model, one warehouse, one feed. This is a demonstration of a traversal that
  generalises, not a product with production hardening.

## Repository layout

```
culprit/           the agent
  agent.py           reasoning loop, tools, system prompt
  warehouse.py       deterministic SQL evidence (every number originates here)
  datahub_graph.py   GraphQL access to ML entities and lineage
  mcp_bridge.py      stdio client to DataHub's MCP server
  writeback.py       raiseIncident, save_document, column annotation
  cli.py             entrypoint
pipeline/          the real data stack under test
  load_raw.py        real TLC parquet into DuckDB
  dbt/               real transforms, including the defect
  train_model.py     production model and counterfactual control
  score_batch.py     real scoring, real error measurement
  emit_ml_lineage.py the ML half of the DataHub graph
  ingest_dbt.yml     DataHub native dbt ingestion recipe
scripts/           independent verification of the incident
examples/          sample outputs from real runs
docs/              strategy and demo script
```

## Design note: the model is the engine

The reasoning is the model's. Which features look wrong, which columns to walk
back to, what a change in those values means, and whether a hypothesis survives
are all decided by the agent. Which *vendor* of model is deliberately not
Culprit's business: the agent is provider-agnostic and the tool loop is
identical across OpenAI, Anthropic and any OpenAI-compatible local server.

The deterministic layer exists for one narrow reason: **the model is never asked
to produce a number.** Every dollar figure and row count is returned by SQL and
handed to the agent as a fact. That is a guardrail on the engine, not a
replacement for it, and it closes the obvious failure mode where a language model
fabricates a plausible-looking financial impact.

## License

[Apache 2.0](LICENSE).

NYC TLC trip data is published by the New York City Taxi and Limousine Commission
as a public dataset.
