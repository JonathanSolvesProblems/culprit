# Devpost submission text

Paste-ready. Category: **Production ML Agents**.

---

## Tagline

A stack trace for model decay. It names the column, prices the damage, and opens
the PR.

Short variant if the field is character-capped:
`A stack trace for model decay - it names the column, prices the damage, opens the PR.`

---

## Inspiration

Model monitoring finds the cause inside the model. Data observability finds it
inside the warehouse. Both do that well now. Neither holds the fact that decides
this case, because it sits on the boundary between them: **which category values
the deployed model was actually fitted on.**

I went looking for a real example rather than inventing one, and found it in the
public NYC Taxi and Limousine Commission feed. In December 2024 a new taxi vendor
started reporting trips under `VendorID = 7`. It showed up with 230 trips, about
six thousandths of one percent of that month. By June 2025 it was 67,573 trips.

Freshness, volume, null-rate and schema checks all stayed green. The column kept
its name, its integer type, its zero null rate and its normal row volume, and the
only visible trace at the source is a single column's maximum value going from
6 to 7.

I ran the fuller sweep too, rather than only the checks that flatter the story,
and four metrics fire, two of which are false alarms that predate the defect by
months (a distinct-value count tracking row volume, and pre-existing bad geometry
in the raw feed). Of the two that are real: `unique_count` on `vendor_id` goes 3 to 4 in December
2024, which is the same integer the max already shows and names no model.
`zero_count` on the derived speed feature climbs, but in December 2024 that is
255 rows out of 3,502,209, against a baseline already swinging between 33 and 43.
It does not become unmissable until March 2025, a quarter after the model started
serving the new vendor wrong. And when it fires it says *some speeds are zero*.

That is the actual argument. Detection was never the hard part. Attribution is.

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
  exposing 21 tools. Six are allowlisted into the investigation loop; the
  mutation tools are deliberately held back for the explicit write-back step.
  Culprit does not reimplement catalog access.
- **A real sklearn model** trained on 6.88M rows, plus a counterfactual control.
- **Real write-back**: `raiseIncident` (`CUSTOM` / "Semantic drift" / `HIGH`),
  `save_document` for the full trace, and an annotation appended to the offending
  source column. The incident lands on the source dataset rather than the model
  because DataHub rejects `mlModel` URNs as incident resources outright. That is
  a platform gap I hit, documented, and am filing upstream.
- **A generated fix that has to prove itself.** `culprit fix` locates the
  transformation at fault, patches it, then runs `dbt build` against the real
  warehouse and checks three gates before proposing anything: the build succeeds,
  the affected rows now match a category, and no other segment's row count
  changed. Only then does it open a PR.

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

I drafted a `datahub-ml-lineage` skill for the DataHub Skills registry, then
found that PR #77 on that repo, opened two days before mine by another
contributor, ships a skill with the identical name and path. So I did not file
it. Submitting a near-duplicate of somebody else's open PR would add noise to a
repo already carrying 56 of them, and would read as derivative even though it was
not.

The draft stays in the repo at `contrib/datahub-skills/` as working evidence.

I then checked the rest of my findings against what was already filed, rather than
assuming. The mlModel incident rejection, which I had planned to file, is also
already claimed: datahub#18685 documents it, lists the supported entity types, and
recommends the same workaround Culprit implements. So I am not re-filing that
either. I will add a comment there with the second error message: that PR quotes
`is not a valid destination`, while DataHub Core v1.5.0.6 surfaces the same call
as an aspect-validation error that does not name the allowed set.

What I am filing is the one finding nothing else covers, across 39 open issues on
`acryldata/mcp-server-datahub`: `update_description` with a `column_path` that
matches no field returns `{"success": true}` and writes nothing. That one cost me
an hour, because DataHub's dbt connector creates two sibling datasets and only one
carries `schemaMetadata`.

Two near-duplicates avoided by checking first. The full list of eight verified
findings is in `docs/DATAHUB_FINDINGS.md`.

## Built with

python, datahub, duckdb, dbt, scikit-learn, anthropic-claude, model-context-protocol,
docker, graphql, nyc-tlc-open-data
