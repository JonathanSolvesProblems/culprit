# Submission checklist

Against the organisers' Aug 6 announcement. **Deadline: Monday Aug 10, 21:00 UTC.**
Submissions cannot be edited after the deadline, so the target is Aug 8.

## Required

| # | Requirement | Status | Notes |
|---|---|---|---|
| 1 | Public repo | **BLOCKED** | Currently PRIVATE. Flip at https://github.com/JonathanSolvesProblems/culprit/settings |
| 2 | Apache 2.0 `LICENSE` file at root, detected in the About sidebar | **DONE** | Real file, 11,358 bytes. `gh` confirms `licenseInfo.key = apache-2.0`. Re-verify logged out once public. The announcement calls this the single most commonly missed item. |
| 3 | Demo video under 3 minutes, public on YouTube/Vimeo/Youku | **NOT STARTED** | Script is 85s. Test in an incognito window before submitting. |
| 4 | Project URL judges can test | **DONE** | Public repo with setup instructions is explicitly enough. `culprit replay --animate` runs with no key, no Docker, no warehouse; verified from a cold clone. No hosted instance, deliberately: the DataHub quickstart ships with auth disabled and mutations enabled. |
| 5 | One challenge category | **DECIDED** | **Production ML Agents.** Select it on the form. The entry competes only in this category. |
| 6 | Text description: what it does, how it uses DataHub, the tech | **DONE** | [SUBMISSION.md](SUBMISSION.md), paste-ready. Covers all three. |

## Optional boosts, both claimed

| # | Boost | Status | Notes |
|---|---|---|---|
| 7 | Sample outputs in `examples/` | **DONE** | 15 artifacts plus [examples/README.md](../examples/README.md) as a reading order. Everything is from a real run. |
| 8 | Open-source contribution to DataHub | **PENDING, needs you** | Two near-duplicates were declined after checking (skills PR #77, and datahub#18685 which pre-empts the mlModel incident finding). The one genuinely unclaimed finding is filed instead. See below. |

## Also free

| # | Item | Status |
|---|---|---|
| 9 | Swag raffle round 2, closes Aug 10 06:59 UTC | **NOT DONE.** Entry details pinned in `#agent-hackathon` on DataHub Community Slack. Costs a minute. |

---

## What only you can do

**1. Screenshots.** DataHub is starting now. Once `http://localhost:9002` is up
(`datahub` / `datahub`), resolve any leftover incident, re-run
`python -m culprit.cli investigate --write-back` so the incident carries the
corrected per-row figure, then capture, in priority order:

1. the `nyc_fare_predictor` model page framing `vendors_in_training_data = 1,2,6`
   (this is the Devpost cover)
2. the incident on `raw.yellow_trips`: CUSTOM / "Semantic drift" / HIGH, red badge
3. column-level lineage on `fct_trip_features` with `vendor_id` expanded
4. the appended `vendor_id` column description

Inline each next to the claim it proves in the README. The repo currently
contains zero images.

**2. Two OSS filings.** Body text is in [DATAHUB_FINDINGS.md](DATAHUB_FINDINGS.md).

- **File:** an issue on https://github.com/acryldata/mcp-server-datahub/issues for
  finding #5: `update_description` with a `column_path` that matches no field
  returns `{"success": true}` and writes nothing. Unclaimed across 39 open issues.
- **Comment:** on https://github.com/datahub-project/datahub/pull/18685 with the
  second error message DataHub Core v1.5.0.6 produces for the same call.
- **Do not** file the `datahub-ml-lineage` skill: PR #77 has the identical name
  and path, opened two days before mine.

Then flip the OSS section of `SUBMISSION.md` to past tense with the live URLs.

**3. Record the voiceover** using the corrected beat order in
[DEMO_SCRIPT.md](DEMO_SCRIPT.md). The peak is the `vendors_in_training_data =
1,2,6` line, not the gate table.

**4. Submit Aug 8.** Not Aug 10. No edits after the deadline.

---

## Before you submit

Run this. It checks every headline figure against the committed artifacts and
that no retired claim has crept back:

```bash
python scripts/check_claims.py
```

It must print `PASS`. Two review rounds caught this script itself reporting
"absent" about text that was present, so it is worth re-running after any prose
edit.
