# Culprit: locked strategy

Internal document. Written at Phase 0, before the first line of code. Every scope
decision gets checked against the uniqueness claim below. If a feature does not
strengthen that sentence, it is deferred.

## Category

**Production ML Agents** (challenge #3).

Chosen deliberately, not by default. Text-to-SQL is already shipped open source as the
DataHub Analytics Agent. Auto-documentation overlaps the shipped `/improve-context`
command and DataHub Cloud AI docs. Dashboard root-cause is already shipped in DataHub
Cloud via column-level lineage plus Smart Assertions. Production ML Agents is the only
challenge whose entry cost (you must emit `mlModel`, `mlFeature`, `mlFeatureTable`, and
`dataProcessInstance` yourself, because no sample datapack ships them) thins the field.
Four challenge winners at $3,000, one per category.

## The one sentence

**Culprit is a stack trace for model decay.**

## Uniqueness claim (locked)

> No shipped tool, and no likely competing entry, connects a *semantic* change in an
> upstream column to the specific production model that absorbed it, the exact retrain
> that baked it in, and the dollar error in the predictions served since.

Why it holds:

- **Model monitoring** (Evidently, Arize, WhyLabs, Fiddler) detects that a feature
  distribution moved, and increasingly traces it back through features. Corrected
  Aug 2026: the earlier wording here said these tools "have no lineage", which is
  no longer true and was load-bearing. What they do not hold is the deployed
  model's fitted category set.
- **Data observability** (Monte Carlo, Sifflet, Metaplane) traces incidents through
  warehouse lineage, and Monte Carlo now does so agentically. It stops at the
  warehouse boundary; the model is not in its graph.
- **Lineage tools** (including DataHub's own column-level lineage) show the path but
  have no signal that anything is wrong.
- **DataHub Smart Assertions** detect statistical anomalies in freshness, volume, and
  column health. The failure class Culprit targets produces **none** of those signals.
- Only DataHub's end-to-end ML lineage holds both the path and the ML terminals
  (feature, feature table, model, training run, deployment). Nothing walks it today.

## The wedge: the class of failure where every detector is green

Volume normal. Freshness normal. Null rate normal. Schema unchanged. Pipeline passing.
A column changed *meaning*, not shape:

- a new enum value appears that an encoder was never taught
- a unit changes (cents to dollars) with no type change
- a backfill rewrites history and leaks future information into training
- an upstream join changes cardinality
- a glossary definition behind a label is edited

## Headline number (hypothesis, written before building)

> **"Every monitor green. $X of mispriced fares. Root cause in Y seconds."**

Rules on this number:
- Expressed in **dollars and seconds**, never in benchmark units, ticks, or accuracy on
  a corpus I authored.
- The human baseline gets quoted alongside it.
- If the honest number is small, the *scenario* changes, not the number.

## AI is the engine, determinism is the rail

Leading with "deterministic core, advisory AI" has now lost twice. Inverted here:

- **Agents are the engine.** Hypothesis generation, traversal strategy, semantic
  diffing of column meaning, and the root-cause narrative are all model work.
- **Determinism is the safety rail only.** Every dollar figure and every row count is
  computed in SQL against the warehouse and passed to the model as a fact. The model
  never invents a number. This is a guardrail, not the pitch.

## Contribution back to the graph

The judging criteria says strong submissions go beyond reading. Culprit writes:

- a DataHub **Incident** via `raiseIncident`, raised on the model's source dataset
  rather than the `mlModel` itself, because DataHub rejects `mlModel` URNs as
  incident resources (finding #1 in `DATAHUB_FINDINGS.md`)
- a root-cause **Document** (`save_document`) linked to the model and the source column
- an **annotation appended to the offending source column**, so the next person who
  opens it inherits the finding (`update_description` via MCP)
- the **ML lineage itself** (`mlModel`, `mlFeature`, `mlFeatureTable`,
  `dataProcessInstance`), emitted by Culprit's own ingestion, since no datapack has it

## Everything runs live (no simulator anywhere)

Shipping the judged artifact in sim mode is the single most expensive mistake I have
made, twice. Non-negotiable here:

- real NYC TLC trip records in a real DuckDB warehouse
- real dbt transforms producing real feature tables
- a real trained model producing real predictions
- real ML lineage emitted through the real DataHub Python SDK
- a real DataHub OSS instance, real MCP server, real GraphQL write-back
- the fault is a **real change to real data**, not a mocked event

## Parallel track (OSS bonus criterion)

Contribute a `datahub-ml-lineage` skill to `datahub-project/datahub-skills`. Five
catalog skills exist today and none cover ML. Any real bug or documentation gap found
while building becomes an issue or PR against DataHub Core.

## Screened out at Phase 0

Anything shaped like a governance or approval gate sitting in front of other agents.
That shape is 0 for 2 and would have been the third repeat.
