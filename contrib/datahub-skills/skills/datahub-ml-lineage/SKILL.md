---
name: datahub-ml-lineage
description: |
  Use this skill when the user wants to work with machine-learning entities in DataHub: registering models, feature tables and training runs, tracing lineage from a production model back to the raw columns that feed it, or answering model-aware impact questions. Triggers on: "which models depend on this table", "what features feed model X", "register this model in DataHub", "trace this model back to source", "what did this model train on", "model impact analysis", "ML lineage", "feature lineage", "training run", "mlModel", "mlFeature", "mlFeatureTable", or any request connecting datasets to models.
user-invocable: true
min-cli-version: 1.5.0.1rc1
allowed-tools: Bash(datahub *)
---

# DataHub ML Lineage

Dataset lineage stops at the warehouse boundary. This skill continues the walk
into the ML layer: dataset -> mlFeature -> mlFeatureTable -> mlModel, plus the
dataProcessInstance that records which data a model actually trained on.

## Not this skill

- Plain dataset-to-dataset lineage, impact analysis on tables or dashboards:
  use **datahub-lineage**.
- Finding assets, searching by tag or domain: use **datahub-search**.
- Adding descriptions, tags or glossary terms: use **datahub-enrich**.
- Freshness, volume and assertion health: use **datahub-quality**.

Use this skill when a **model** is at one end of the question.

## The entities

| entity | what it represents | key aspect |
|---|---|---|
| `mlModel` | one trained, versioned model | `mlModelProperties` |
| `mlModelGroup` | a family of model versions | `mlModelGroupProperties` |
| `mlFeature` | a single model input | `mlFeatureProperties` |
| `mlFeatureTable` | the set of inputs a model consumes | `mlFeatureTableProperties` |
| `mlPrimaryKey` | join key of a feature table | `mlPrimaryKeyProperties` |
| `dataProcessInstance` | one training run | `mlTrainingRunProperties` |

The edges that matter:

```
dataset --(mlFeatureProperties.sources)--> mlFeature
mlFeature --(mlFeatureTableProperties.mlFeatures)--> mlFeatureTable
mlFeature --(mlModelProperties.mlFeatures)--> mlModel
mlModel --(mlModelProperties.groups)--> mlModelGroup
mlModel --(mlModelProperties.trainingJobs)--> dataProcessInstance
dataset --(dataProcessInstanceInput.inputs)--> dataProcessInstance
```

## Step 1: Identify the target

If the user names a model, resolve it to a URN first. Model URNs look like:

```
urn:li:mlModel:(urn:li:dataPlatform:<platform>,<name>,<env>)
urn:li:mlFeature:(<feature_table_name>,<feature_name>)
urn:li:mlFeatureTable:(urn:li:dataPlatform:<platform>,<table_name>)
```

Note that `mlFeature` URNs do **not** carry a platform. They are scoped by
feature table name only. This trips people up.

```bash
datahub get --urn "urn:li:mlModel:(urn:li:dataPlatform:mlflow,churn_v3,PROD)"
```

To list every model in the instance, search with the `MLMODEL` entity type
rather than guessing URNs.

## Step 2: Walk model -> features -> source columns

Read `mlModelProperties.mlFeatures` to get the model's inputs, then read each
`mlFeatureProperties.sources` to get the datasets behind them.

```bash
datahub get --urn "<model_urn>" --aspect mlModelProperties
datahub get --urn "<feature_urn>" --aspect mlFeatureProperties
```

`sources` holds **dataset** URNs, not schemaField URNs. If you need
column-level precision, continue with dataset column lineage from
**datahub-lineage** once you reach the dataset, or read the feature's
`customProperties` where the emitting job recorded the source column.

## Step 3: Walk the other direction for impact analysis

"Which production models break if I change this column?" is the question this
skill exists for. Start at the dataset and search for features whose `sources`
contain it, then follow those features to their models.

Always report the answer as a path, not just an endpoint. A user who asks what
depends on a column needs to see the hops in between so they can judge whether
the dependency is real.

## Step 4: Check what the model actually trained on

The `dataProcessInstance` is the most under-used entity in the ML metamodel and
usually the most valuable. It records the training run: input datasets, metrics,
hyperparameters and run status.

```bash
datahub get --urn "urn:li:dataProcessInstance:<run_id>" --aspect mlTrainingRunProperties
datahub get --urn "urn:li:dataProcessInstance:<run_id>" --aspect dataProcessInstanceInput
```

A model can be perfectly healthy against its training distribution and badly
wrong in production because the serving data has changed. Comparing the training
run's inputs against current data is how you find that.

## Step 5: Registering ML entities

Use the Python SDK. The metadata classes are the reliable path; several fields
are records rather than plain strings and this is the most common source of
emission failures.

```python
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.emitter.mce_builder import make_ml_feature_urn, make_ml_feature_table_urn
from datahub.metadata.schema_classes import (
    MLFeaturePropertiesClass, MLFeatureTablePropertiesClass,
    MLModelPropertiesClass, VersionTagClass, MLMetricClass, MLHyperParamClass,
)

emitter = DatahubRestEmitter(gms_server="http://localhost:8080")

feature_urn = make_ml_feature_urn("my_feature_table", "avg_speed_mph")
emitter.emit(MetadataChangeProposalWrapper(
    entityUrn=feature_urn,
    aspect=MLFeaturePropertiesClass(
        description="Average speed over the trip.",
        dataType="CONTINUOUS",
        sources=["urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.features,PROD)"],
    ),
))
```

### Field types that are records, not strings

| field | wrong | right |
|---|---|---|
| `MLModelProperties.version` | `version="1.4.0"` | `version=VersionTagClass(versionTag="1.4.0")` |
| `MLModelProperties.created` | epoch int | `TimeStampClass(time=...)` |
| `MLModelProperties.trainingMetrics` | `{"mae": 2.1}` | `[MLMetricClass(name="mae", value="2.1")]` |
| `MLModelProperties.hyperParams` | `{"lr": 0.1}` | `[MLHyperParamClass(name="lr", value="0.1")]` |

`MLModelGroupProperties` does **not** accept a `platform` argument. The platform
is already encoded in the group URN.

Metric and hyperparameter values are **strings**, not numbers. Passing a float
raises an Avro type exception that names the whole record and not the offending
field, which makes it hard to debug.

## Common mistakes

- Assuming `mlFeature` URNs include a platform. They do not.
- Passing `version` as a string. It is a `VersionTag` record.
- Passing numeric metric values. They are strings.
- Expecting `trainingJobs` to be queryable on `MLModelProperties` through
  GraphQL. It exists on the aspect but is not exposed as a GraphQL field, so
  query it through the aspect API or via relationships.
- Reporting only the model at the end of an impact walk. Show the path.
- Treating a green pipeline as evidence a model is healthy. Freshness, volume
  and null checks pass while a model silently degrades whenever the *meaning* of
  an input changes rather than its shape.

## Red flags

Stop and tell the user rather than guessing when:

- No `mlModel` entities exist in the instance. ML lineage has to be emitted; no
  sample datapack ships it. Say so plainly instead of returning an empty result.
- A feature's `sources` is empty. The lineage was never wired and the walk cannot
  continue. Do not infer the source from the feature's name.
- The model has no `dataProcessInstance`. You cannot answer what it trained on.

## Remember

The question this skill uniquely answers is **"which production model absorbed
this change, and what did it train on?"** Dataset lineage alone cannot answer it,
and model monitoring alone cannot answer it. Both halves live in DataHub, and
only walking across the boundary between them gets you there.
