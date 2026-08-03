# Culprit: demo voiceover script

Target length: **75 seconds**. Hard ceiling: 90 seconds.
One feature, shown deeply, on real data. No feature tour.

**Every number below is now the real measured value**, replacing the
placeholders this file was written with before the build. Where the original
placeholders turned out to be wrong, they were corrected against the data rather
than kept because they sounded better. See [the correction log](#correction-log).

---

## [0:00 - 0:12] Cold open: everything is green

**On screen:** the `nyc_fare_predictor` model page in DataHub, then the monitor
panel. Freshness green. Volume green. Null rate 0.0%. Schema unchanged.

> "This is a fare model running on real New York City taxi data. Freshness,
> volume, null rate, schema: all green. Nothing failed. Nothing alerted."

## [0:12 - 0:26] The reveal

**On screen:** the per-vendor error table, vendor 7 row highlighted.

> "And on sixty-six thousand of last month's trips it carries forty-five percent
> more error than the same model retrained with that vendor included. It has been
> drifting for six months and nobody knows."

## [0:26 - 0:36] Why nothing caught it

**On screen:** the monitor table, cutting to the `max` column going 6, 6, **7**.

> "Here is the whole signal at the source. One integer. A column's maximum value
> went from six to seven."

**Beat.** Do not overclaim here. The concession is the stronger line:

> "Some checks do eventually notice. None of them tell you which model broke,
> which retrain baked it in, or what it cost."

## [0:36 - 0:58] Culprit runs

**On screen:** terminal. The trace renders hop by hop.

> "Culprit walks DataHub's end-to-end ML lineage backwards from the model, through
> the feature table, through the dbt transforms, to the raw column."

**On screen:** the trace lands.

> "In December, the TLC added a new vendor. Our one-hot encoder was written before
> that vendor existed, so its trips now claim to come from no vendor at all. And
> because that vendor reports pickup and dropoff at the same second, the
> divide-by-zero guard quietly pins their speed to zero miles an hour."

**Beat.** This is the line the whole demo exists for:

> "The null-safety guard is what hid it. Without it, the column would have gone
> null and someone would have been paged."

## [0:58 - 1:08] The write-back

**On screen:** the incident appearing on the model page in DataHub.

> "Culprit raises the incident on the model in DataHub, with the evidence
> attached, so the next engineer or the next agent inherits the answer instead of
> rediscovering it."

## [1:08 - 1:18] Close

**On screen:** the headline number, full frame.

> "Ninety thousand dollars of prediction error in a single month, on a stack where
> every monitor said green. Culprit: a stack trace for model decay."

---

## The measured numbers

| claim in script | measured value | source |
|---|---|---|
| "sixty-six thousand trips" | 66,146 | SQL, real 2025-06 |
| "forty-five percent more error" | $4.606 vs $3.167 MAE = 45.4% worse | counterfactual control |
| "drifting for six months" | first appeared 2024-12, scored 2025-06 | real TLC feed |
| "one integer, six to seven" | `max(vendor_id)` 6 -> 7 in 2024-12 | SQL |
| "speed pinned to zero" | `avg_speed_mph` = 0.000 vs 11.59 baseline | SQL |
| "a dollar thirty-seven a ride" | $1.3655 (difference-in-differences) | `examples/04_measured_impact.json` |
| "ninety thousand dollars" | $90,322.36 | difference-in-differences |

The per-trip figure is **$1.37**, never $1.44. $1.3655 x 66,146 = $90,322 checks
out; the naive $1.4386 does not, and mixing the two is the fastest way to look
careless.

## Correction log

Placeholders that the data contradicted, and what replaced them:

- **"overcharging by $2.14 a trip"** was wrong and is cut. The signed bias on
  vendor 7 is **-$0.67**, which is close to vendor 2's **-$0.80**. The model is
  not systematically overcharging those trips. The damage is in error
  *magnitude*, not direction, so the script now says "forty-five percent less
  accurate", which is what was actually measured.
- **"six days"** became **"six months"**. The vendor entered the feed in December
  2024 and the scored month is June 2025.
- **"$18,400"** became **$90,322.36**, and the estimator changed. The naive
  control difference gives $95,158.12; the difference-in-differences figure that
  nets out the control model's data advantage gives $90,322.36. The stricter
  number is the one on screen.
- **"eleven seconds"** is removed until the agent has actually been timed. No
  runtime claim goes in the script before it is measured.
- **"No monitor on earth is watching that"** was an overclaim and is cut. Running
  the full sweep shows two metrics do fire: `unique_count` on `vendor_id` (3 to 4,
  in 2024-12) and `zero_count` on `avg_speed_mph`. The honest version is stronger,
  because at 2024-12 the zero count is 255 rows out of 3,502,209, against a
  baseline already swinging 33 to 43, and it does not become unmissable until
  2025-03, a quarter after the damage started. Neither signal names the model, the
  retrain, or the cost. Detection was never the hard part. Attribution is.

## What is deliberately NOT in this demo

- No feature tour, no settings screens, no architecture narration.
- The dollar figure is stated once, at the end, and never repeated.
- The second real finding (vendor 6) is in the README, not the demo. It is true
  and worth reporting, but it is not the story.
