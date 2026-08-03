# Devpost submission text

Paste-ready. Category: **Production ML Agents**.

---

## Tagline

A stack trace for model decay.

---

## Inspiration

Every monitoring tool I have used tells you **what** moved. None of them tell you
**why**, because none of them have lineage.

I went looking for a real example rather than inventing one, and found it in the
public NYC Taxi and Limousine Commission feed. In December 2024 a new taxi vendor
started reporting trips under `VendorID = 7`. It showed up with 230 trips, about
six thousandths of one percent of that month. By June 2025 it was 67,573 trips.

Nothing about that change is detectable by conventional monitoring. The column
kept its name, its integer type, its zero null rate and its normal row volume.
The only visible trace in the entire feed is a single column's maximum value
going from 6 to 7.

Meanwhile any fare model trained before December 2024 has a one-hot encoder that
was written when only vendors 1, 2 and 6 existed. Every vendor-7 trip now tells
that model it came from no vendor at all.

## What it does

Culprit is handed a production model and a vague human complaint ("quotes have
drifted, nobody knows why"). It walks DataHub's end-to-end ML lineage backwards
from the model, through the feature table, through the dbt transforms, to the raw
source column, finds the change in **meaning**, proves that standard monitors
would not have fired, measures the damage in dollars, and writes the finding back
into DataHub as an incident so the next engineer or agent inherits the answer.

On the real June 2025 data:

> **$90,322 of attributable prediction error in a single month, across 66,146
> real trips, while freshness, volume, null-rate and schema checks all stayed
> green.**

There is a second defect stacked on the first, and I did not plant that either.
Vendor 7 reports pickup and dropoff at the same second, so every one of its trips
has a duration of exactly zero. The feature model guards against division by zero
the way everyone does:

```sql
coalesce(trip_distance / nullif(trip_minutes / 60.0, 0), 0) as avg_speed_mph
```

The null-safety guard is what hides the corruption. Without it the column would
have gone NULL and someone would have been paged. With it, the column stays clean
and confidently reports 0 mph for 66,146 trips.

## How I built it

The whole point was that nothing could be simulated, so the stack is real end to
end:

- **19.3M real NYC TLC trip records** across five months, loaded at true published
  volumes into DuckDB. No sampling anywhere.
- **Real dbt transforms** (dbt-duckdb) containing the actual defect.
- **DataHub's native dbt connector** parses the real `manifest.json` and
  `catalog.json` to produce genuine column-level `fineGrainedLineage`. The dataset
  lineage is ingested build output, not something I asserted.
- **The ML half of the graph, contributed by this project.** No DataHub sample
  datapack ships ML entities, so I emit 13 `mlFeature`s, an `mlFeatureTable`, an
  `mlModelGroup`, an `mlModel` and a `dataProcessInstance` training run through
  the DataHub Python SDK. Each feature records the source column it derives from,
  which is what makes the walk from model back to column possible.
- **DataHub's own MCP server** (`mcp-server-datahub` 0.6.0) launched over stdio,
  exposing 19 tools to the agent. Culprit does not reimplement catalog access.
- **A real sklearn model** trained on 6.88M rows, plus a counterfactual control.
- **Real write-back**: `raiseIncident` on the affected model, `save_document` for
  the full trace, and an annotation on the offending source column.

The agent is genuinely uninstructed. Nothing about taxis, vendors, or one-hot
encoding appears anywhere in its system prompt. It gets a model URN, tools, and a
method, and it works the problem.

## Measuring the damage honestly

A dollar figure is easy to inflate, so this one is built to survive scrutiny.

Two models train with identical hyperparameters. The production model sees
2024-06 and 2024-09 (vendors 1, 2, 6). The control sees those plus 2025-03, and
its encoder knows vendor 7. Both score the full real June 2025 month.

The obvious objection is that the control saw more data, and fresher data, so
some of its advantage is not the fix. That objection is correct, which is why the
naive difference is not the headline. The control's unearned advantage is
directly measurable on the segments that have no encoding defect: across
3,840,878 unaffected rows it is $0.0731 per row. Subtracting it gives a
difference-in-differences estimate:

```
(prod_mae_v7 - ctrl_mae_v7) - (prod_mae_baseline - ctrl_mae_baseline)
= $1.4386 - $0.0731 = $1.3655 per trip
$1.3655 x 66,146 trips = $90,322.36
```

Both estimators are returned. The stricter one is the headline.

The language model is never asked to produce a number. Every dollar figure and
row count is computed in SQL and handed to the agent as a fact, which closes the
obvious failure mode where an LLM fabricates a plausible-looking financial impact.

## Challenges I ran into

**The volume monitor fired, and it was my fault.** My first loader down-sampled
the training months to 750k rows while loading serving months in full. That made
row volume swing 82%, which would have made a conventional volume monitor fire
and destroyed the central claim. The claim was being tested against an artifact
of my loader rather than the real feed. I reloaded every month at true published
volume, moved sampling out of the warehouse entirely, and the swing dropped to
18%, which is ordinary seasonal variation.

**My demo script had a number that was simply wrong.** I wrote the voiceover
before building, as a focus discipline, with placeholder figures. One of them
claimed the model was "overcharging by $2.14 a trip". When I measured the signed
bias it was **-$0.67** on vendor 7, close to vendor 2's **-$0.80**. The model is
not systematically overcharging those trips at all. The damage is in error
magnitude, not direction. The script now says "45% less accurate", which is what
was actually measured, and the repo carries a correction log of every placeholder
the data contradicted.

**DataHub SDK type surprises.** `MLModelProperties.version` is a `VersionTag`
record, not a string. Metric and hyperparameter values are strings, not numbers.
`MLModelGroupProperties` does not accept a `platform` argument. Each of these
raises an Avro exception that names the whole record and not the offending field.
Those findings became the contributed skill.

## Accomplishments I am proud of

Finding a real incident instead of planting one. The fault in this repository
genuinely happened in a public dataset, and anyone can verify it in about a minute
with `python scripts/scan_tlc_semantics.py`.

Reporting the inconvenient finding. Vendor 6 shows a $1.387 attributable gap and
an implausible 61.95 mph average speed. I did not plant it and did not know about
it before building. It is worth only about $1,900 on 1,395 trips so it does not
change the headline, but it is in the README because suppressing a real finding
would be worse than a slightly messier result.

## What I learned

The most expensive data defects are invisible to structural monitoring by
construction. Freshness, volume, null rate and schema checks all watch the
**shape** of data. When the **meaning** changes and the shape does not, there is
nothing for them to see, and the defensive coding practices that keep pipelines
clean are often the very thing that hides the corruption.

## What is next

Unit changes and backfill-driven training leakage are described in the agent's
method but only the new-categorical-value path is exercised end to end here. The
same traversal handles them; they need their own real examples.

## Open-source contribution

I contributed a `datahub-ml-lineage` skill to the DataHub Skills registry. Five
catalog skills exist today and none cover ML. It documents the ML metamodel, the
model-to-column walk, the impact-analysis direction, and the SDK type traps above
that cost me real debugging time.

## Built with

python, datahub, duckdb, dbt, scikit-learn, anthropic-claude, model-context-protocol,
docker, graphql, nyc-tlc-open-data
