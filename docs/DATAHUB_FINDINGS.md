# Findings against DataHub while building Culprit

Things that cost real debugging time, verified against a live DataHub Core
v1.5.0.6 instance with `mcp-server-datahub` 0.6.0. Recorded here because they are
reusable by anyone else building ML-aware tooling on DataHub, and because most of
them belong upstream as documentation or issues.

Everything below was confirmed by GraphQL introspection or by executing the call
and reading the result back, not inferred from docs.

---

## 1. Incidents cannot be raised on `mlModel` entities

**Impact: high.** This is the one that changed Culprit's design.

`raiseIncident` rejects an mlModel URN:

```
java.lang.RuntimeException: Invalid format for aspect: incident
 Cause: ERROR :: /entities/0 :: "Provided urn urn:li:mlModel:(...)" is invalid:
        Entity type for urn ... is not supported
```

`MLMODEL` is a first-class `EntityType`, and mlModel exposes health-adjacent
fields, but the incident aspect does not accept it as a resource. In practice
this means **a degraded model cannot carry its own incident**, which is exactly
the thing an ML observability workflow wants to express.

Workaround used here: raise the incident on the upstream *dataset*, which does
support incidents and surfaces the red health badge in search, and name the
affected model in the incident body.

Worth an upstream issue: either support ML entities as incident resources, or
document the supported resource types explicitly. The current failure is a
500-style runtime exception rather than a validation message naming the allowed
set.

## 2. `IncidentType` has no data-quality member

Introspected enum:

```
FRESHNESS, VOLUME, FIELD, SQL, DATA_SCHEMA, OPERATIONAL, CUSTOM
```

`DATA_QUALITY` does not exist, which is the obvious first guess. Use `CUSTOM`
with a `customType` string; the Incident type exposes `customType`, so the UI
renders your own label. Culprit sets `customType: "Semantic drift"`.

## 3. `priority` is an enum, not an integer

`IncidentPriority` is `LOW | MEDIUM | HIGH | CRITICAL`. Passing `priority: 1`,
which reads naturally as "P1", fails schema validation.

## 4. MCP tool failures do not raise

`ClientSession.call_tool` returns a result with `isError=True` rather than
throwing. Any caller wrapping tool calls in `try/except` will therefore treat a
failed mutation as a success and report that it wrote something it did not. This
is the highest-severity footgun in the list because it fails *silently*.

Culprit checks `result.isError` explicitly in `culprit/mcp_bridge.py` and raises.

## 5. Only the dbt sibling carries `schemaMetadata`

DataHub's dbt connector creates two datasets per model: the dbt node and the
target-platform table. With only dbt ingested, and no separate warehouse
ingestion, **the dbt node holds the columns and the target-platform dataset is a
lineage stub with zero fields.**

Consequence: `update_description` with a `column_path` against the
target-platform URN returns `{"success": true}` and writes nothing observable,
because the field does not exist on that entity. Verified on this instance:

| dataset | schema fields |
|---|---|
| `dbt,nyc_fares.warehouse.raw.yellow_trips` | 14 |
| `duckdb,warehouse.raw.yellow_trips` | 0 |

Anything column-level must target the dbt URN. A warning when a `column_path`
does not match any field on the entity would have saved an hour.

## 6. `save_document` requires `document_type`

Required arguments are `document_type`, `title`, `content`. Omitting
`document_type` fails. Also pass `related_assets` with the entity URNs, otherwise
the document is created but is not linked from any asset page and is effectively
unfindable.

## 7. ML metamodel type traps in the Python SDK

Each of these raises an Avro exception that names the whole record rather than
the offending field, which makes them slow to diagnose:

| field | wrong | right |
|---|---|---|
| `MLModelProperties.version` | `"1.4.0"` | `VersionTagClass(versionTag="1.4.0")` |
| `MLModelProperties.trainingMetrics` | `{"mae": 2.1}` | `[MLMetricClass(name="mae", value="2.1")]` |
| `MLModelProperties.hyperParams` | `{"lr": 0.1}` | `[MLHyperParamClass(name="lr", value="0.1")]` |
| `MLModelGroupProperties` | `platform=...` | no `platform` argument; it is in the URN |

Metric and hyperparameter values are **strings**, not numbers.

`mlFeature` URNs carry no platform: they are `urn:li:mlFeature:(<table>,<name>)`,
unlike `mlFeatureTable` which does include one.

## 8. `trainingJobs` is not exposed on the GraphQL `MLModelProperties`

It exists on the aspect and round-trips through the SDK, but selecting it in
GraphQL fails with `Field 'trainingJobs' in type 'MLModelProperties' is
undefined`. Read it via the aspect API or via relationships instead.

Related: `MLModelProperties.version` is a `String` in GraphQL but a `VersionTag`
record in the aspect model, so the same field name needs different handling
depending on which API you are on. A subselection on it fails with
`SubselectionNotAllowed`.

---

## Intended contributions

Checked against what is already filed, on Aug 4 2026, before filing anything.

**Item 1 is already claimed, and I am not re-filing it.**
[datahub#18685](https://github.com/datahub-project/datahub/pull/18685),
*"docs(incidents): list the entity types that support incidents"*, was opened on
2026-07-28 by `cnpierrepapi`. It documents the same mlModel rejection, lists the
supported set (`dataset, dataJob, dataFlow, chart, dashboard, service, aiAgent`),
and recommends the same workaround Culprit already implements: raise the incident
on the dataset the model trained on. Filing my own version a week later would be
a near-duplicate, which is exactly the thing I declined to do with the skills PR.

What I will add there instead is a **comment with an independent reproduction on a
different surface**: #18685 hit it through the GraphQL mutation, Culprit hit it
through the SDK aspect emit, which fails with `Invalid format for aspect:
incident` rather than the `is not a valid destination` message. Same gap, second
code path, useful to whoever fixes it.

**Item 5 is genuinely unclaimed, and is the one I am filing.**
`acryldata/mcp-server-datahub` has 39 open issues and none covers it: calling
`update_description` with a `column_path` that matches no field on the entity
returns `{"success": true}` and writes nothing observable. It cost an hour of real
debugging here, because the dbt connector creates two sibling datasets and only
one of them carries `schemaMetadata`:

| dataset | schema fields |
|---|---|
| `dbt,nyc_fares.warehouse.raw.yellow_trips` | 14 |
| `duckdb,warehouse.raw.yellow_trips` | 0 |

The ask is small and concrete: warn or error when `column_path` resolves to no
field on the target entity, instead of reporting success.

### A skill I drafted and deliberately did not file

[`contrib/datahub-skills/skills/datahub-ml-lineage/SKILL.md`](../contrib/datahub-skills/skills/datahub-ml-lineage/SKILL.md)
encodes items 7 and 8 above. I did not open a PR for it: **PR #77 on
`datahub-project/datahub-skills`, opened 2026-08-02, already adds a skill with the
identical name and path**, and that repo has 56 open PRs with one merge. Filing a
near-duplicate two days later would add noise and read as derivative.

The draft stays here as working evidence, and the effort went upstream to the
issue above, which nothing else claims. Noting this because finding out from a
judge would be worse than saying it first.
