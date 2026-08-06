# Sample outputs

Every file here was produced by a real run against the real stack. Nothing is
hand-written, and nothing was edited after the fact. Regenerate the derived ones
with `python scripts/generate_examples.py`.

Read them in this order if you have five minutes and do not want to run anything.

## Start here

| file | what it shows |
|---|---|
| **[RESULTS.md](RESULTS.md)** | The whole story in one page: the semantic change, what monitoring saw, per-vendor model behaviour, and the measured impact. |
| **[investigation.json](investigation.json)** | The recorded agent run. 28 tool calls, 13 turns, 151.92s, $0.279 on `gpt-4o`, including every tool call and its arguments. Render it with `python -m culprit.cli replay --animate`, which needs no API key, no Docker and no warehouse. |
| **[terminal_investigation.txt](terminal_investigation.txt)** | Raw terminal capture of that run, unedited. |

## The incident, and why nothing caught it

| file | what it shows |
|---|---|
| [01_semantic_change_detected.json](01_semantic_change_detected.json) | `vendor_id` gaining the value `7` in 2024-12, in the real published NYC TLC feed. This defect was found, not planted. |
| [02_monitor_sweep.json](02_monitor_sweep.json) | The full monitor sweep at both layers, **including the four metrics that do fire**. Two of them are false alarms predating the defect. Testing only the silent ones would have been picking the scoreboard. |
| [03_feature_behaviour_by_vendor.json](03_feature_behaviour_by_vendor.json) | Per-segment behaviour of every model input, with the degenerate ones flagged. Inputs are discovered from the schema, not named in the query. |

## What it cost

| file | what it shows |
|---|---|
| [04_measured_impact.json](04_measured_impact.json) | Both estimators. The naive control difference is $95,158.12; the difference-in-differences figure, which nets out the control model's unearned data advantage, is **$90,322.36**. The stricter one is the headline. |
| [05_secondary_finding_vendor6.json](05_secondary_finding_vendor6.json) | A second, smaller real finding on vendor 6 that I did not plant and did not expect. Reported rather than suppressed. |

## The graph

| file | what it shows |
|---|---|
| [06_ml_lineage_in_datahub.json](06_ml_lineage_in_datahub.json) | The ML entities this project contributes to DataHub: 13 `mlFeature`s, an `mlFeatureTable`, the `mlModel`, and the training run. Note `vendors_in_training_data = 1,2,6`, which is the fact the whole diagnosis turns on. |
| [07_dbt_ingested_lineage.json](07_dbt_ingested_lineage.json) | Three hops of dataset lineage produced by DataHub's **native dbt connector** parsing real build artifacts, dumped without running the agent. Evidence the graph is ingested output, not asserted by this project. |
| [writeback.json](writeback.json) | What the run wrote back: the incident URN, the knowledge document, and the column annotation. |

## The fix, and the one it refused

| file | what it shows |
|---|---|
| [generated_fix.sql](generated_fix.sql) | The accepted patch. It adds a catch-all bucket rather than a special case for vendor 7, so it will not break again on the next new value. |
| [remediation.json](remediation.json) | The three verification gates passing, and the resulting PR. |
| **[remediation_rejected.json](remediation_rejected.json)** | **The most important file here.** The model's first patch was `where vendor_id in (1, 2, 6)`. It compiles, `dbt build` passes, and it makes the symptom vanish by deleting 87,693 rows. The row-count gate caught it and refused to open the PR. Reproduce with `python scripts/capture_rejected_patch.py`. |

## Also

[investigation.fallback.json](investigation.fallback.json) is a rejected run, kept
deliberately. It found **both** damage routes but made no MCP calls, so it failed
the pre-written promotion checklist. Run selection is described in the README
rather than presented as a single clean result.
