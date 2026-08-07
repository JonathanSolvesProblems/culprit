# Culprit: demo voiceover script

**Target 85 seconds. Hard ceiling 90.** Every number below is measured and
sourced. Read the table at the bottom before recording.

Shoot `culprit replay --animate`, not a live investigation. The recorded run took
151.92 seconds, which does not fit in the video, and a live run depends on an API
key and rate limits. Put a caption on screen: *recorded run, 2026-08-04, 28 tool
calls, $0.279*. That is more honest than a staged "live" take and it cannot fail
on camera.

---

## [0:00-0:10] Open on the integer

**Screen:** the monitor table from `examples/RESULTS.md`, cutting to the `max`
column going 6, 6, **7**.

> "In December 2024 a new taxi vendor started reporting trips to New York City.
> One integer changed. A column's maximum value went from six to seven."

## [0:10-0:22] The cost

**Screen:** the per-vendor error table, vendor 7 row highlighted.

> "That one integer cost a fare model a dollar thirty-seven a trip, on
> sixty-six thousand trips, in a single month. Ninety thousand dollars. Measured
> against the same model retrained with that vendor included."

Say the counterfactual out loud. It is what makes the number defensible.

## [0:22-0:28] Why nobody noticed

**Screen:** the monitor sweep table.

> "Freshness, volume, null rate and schema all stayed green. Four checks do fire.
> Two are false alarms from months before the defect existed. The two real ones
> move the same month it starts, by two hundred and fifty-five rows in three and a
> half million, and stay inside the noise until a quarter later. None of them names
> the model, or the cost."

Do not say "no monitor could catch this". It is not true and it is checkable.

## [0:28-0:50] The agent runs, and the fact nothing else holds

**Screen:** terminal, `culprit replay --animate`, trace rendering hop by hop. Hold a
burn-in caption in frame: `gpt-4o · 28 tool calls · 4 through DataHub's MCP server ·
13 turns · 151.92s · $0.279`. Then cut to the dbt column-level lineage graph.

> "Culprit is an agent. It gets a model URN and one vague sentence. No taxis, no
> vendors, no column names anywhere in its prompt. It works out which column
> defines the segments itself, reads that column off the model's features in
> DataHub, then confirms it against real column-level lineage back to the raw table."

**Screen:** the DataHub `nyc_fare_predictor` page, framing `vendors_in_training_data = 1,2,6`.

**This is the peak. Hold it.** It is the one beat no competing entry can copy.

> "And here is the fact no other system holds. The graph recorded that this model
> was trained on vendors one, two and six, while the warehouse was serving vendor
> seven. Its encoder has no slot for a vendor that did not exist when it was written."

## [0:50-1:10] The answer lands in the graph

**Screen:** the incident on the source dataset (CUSTOM / "Semantic drift" / HIGH, with
the evidence body visible), then the knowledge document, then the annotation on the
`vendor_id` column page.

> "It writes the answer back into DataHub. An incident on the dataset, with the
> evidence and the dollars attached. A document. An annotation on the column itself.
> The next engineer who opens vendor_id inherits the answer instead of rediscovering
> it. So does the next agent."

## [1:10-1:20] The PR, and the one it refused

**Screen:** the accepted diff and PR #1, then a fast cut to the rejected diff with the
87,693 figure and a REJECTED stamp. Ten seconds, no lingering on the gate table.

> "The patch it opened adds a catch-all bucket, so it will not break again on the next
> new vendor. Its first attempt deleted eighty-seven thousand rows, and dbt built it
> happily. Culprit ran that one against the real warehouse and refused to open it."

## [1:20-1:25] Close

**Screen:** the headline, full frame.

> "Culprit. A stack trace for model decay."

---

## Numbers, with sources

| spoken | value | source |
|---|---|---|
| "one integer, six to seven" | `max(vendor_id)` 6 → 7 in 2024-12 | `examples/02_monitor_sweep.json` |
| "a dollar thirty-seven a trip" | $1.3655, difference-in-differences | `examples/04_measured_impact.json` |
| "sixty-six thousand trips" | 66,146 | scored month 2025-06 |
| "ninety thousand dollars" | $90,322.36 | `examples/04_measured_impact.json` |
| "255 rows in three and a half million" | 255 of 3,502,209 = 0.0073% in 2024-12 | `examples/02_monitor_sweep.json` |
| "inside the noise until a quarter later" | first exceedance 2024-12 at noise level; unmissable 2025-03 | `examples/02_monitor_sweep.json` |
| "trained on vendors one, two and six" | `vendors_in_training_data = 1,2,6` | `examples/06_ml_lineage_in_datahub.json` |
| "eighty-seven thousand rows" | 87,693 | `examples/remediation_rejected.json` |
| caption: 28 calls, $0.279, 151.92s | recorded run | `examples/investigation.json` |

**$1.37 a trip, never $1.44.** $1.3655 × 66,146 = $90,322. The naive $1.4386 does
not reconcile, and mixing the two reads as careless.

**87,693 rows, not 66,146.** The gate runs over the whole feature table; 66,146 is
the scored month only. Both numbers are correct for different things.

## Do not shoot

- **A live investigation.** 151.92s does not fit, and it depends on a key.
- **A DataHub monitor panel.** OSS v1.5.0.6 does not ship one, and mocking it
  would be exactly the simulated artifact this project claims not to contain.
- **The Validation / Assertions tab.** This dbt project defines zero tests, and
  empty is not the same as green.

## Correction log

Kept deliberately. Every entry is a claim the data contradicted.

- **"overcharging by $2.14 a trip"** cut. Signed bias on vendor 7 is **-$0.67**,
  close to vendor 2's **-$0.80**. The model does not systematically overcharge
  those trips; the damage is error *magnitude*, not direction.
- **"drifted upward"** in the symptom cut for the same reason, since handing the
  agent a direction the evidence contradicts is a leading premise. The committed
  run predates this change and still carries the older wording.
- **"six days"** became **six months**. Vendor entered 2024-12, scored 2025-06.
- **"$18,400"** became **$90,322.36**, and the estimator changed to
  difference-in-differences, which nets out the control's data advantage.
- **"No monitor on earth is watching that"** cut as an overclaim. Four metrics
  fire across the two layers; two are false alarms predating the defect and the
  two real ones are not actionable when the damage starts.
- **The gate table stopped being the climax.** It was the emotional peak at
  1:04-1:20 and labelled "the moment that matters", which is the same shape as
  three previous losses whose demos built toward a proof console. It is now a
  ten-second closing receipt behind the working PR, and the peak is the line the
  graph alone can say: trained on vendors 1, 2 and 6 while serving vendor 7.
- **"eleven seconds"** removed. No runtime claim ships before it is measured; the
  real figure is 151.92s and it is on screen as a caption.
