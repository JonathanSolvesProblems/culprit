"""Stage b-roll and author the vidkit edit plan for the Culprit demo.

vidkit's planner needs an Anthropic key, which this project does not use. The
mapping is authored here instead, which is arguably better anyway: the narration
was written before the b-roll was captured, so which frame belongs under which
sentence is already known and does not need inferring.

Also repairs Whisper's transcription. It mangled the project name to "Pulprate"
throughout, plus a dozen technical terms. Captions are burned in, so those have
to be right.

Usage:  python scripts/build_edit_plan.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIPS = ROOT / "broll" / "clips"
SRC_DIRS = [ROOT / "docs" / "img", ROOT / "broll" / "captures"]
PLAN = ROOT / "broll" / "culprit_demo.edit-plan.json"

# Whisper mishears. Captions are burned in, so every one of these matters.
# Matched case-insensitively on the whole word, longest first.
FIXES = {
    "pulprate": "Culprit",
    "pulprate,": "Culprit,",
    "interchanged": "integer changed",
    "powering": "carrying",
    "fair": "fare",
    "taxes": "taxis",
    "dbd": "dbt",
    "banned": "vanished",
    "trait": "trace",
}

# (clip stem, start, end, lower third, effect)
# Timings come from the actual transcript segments, so each frame lands under
# the sentence that describes it.
SEGMENTS = [
    # nyc.gov blocks headless browsers (Akamai, HTTP 403), so the TLC data
    # dictionary cannot be captured here. The opener uses our own detection of
    # the same change instead. Swap in a manual screenshot of the dictionary if
    # one is taken; the slot is the first two entries.
    ("results_semantic_change",  0.0,  10.7, "The change, in the real feed", "none"),
    ("semantic_change_json",    10.7,  15.1, "Detected, not planted",       "none"),
    ("results_vendors",         15.1,  24.9, "Measured against a control",  "none"),
    ("results_monitors",        24.9,  42.1, "Every standard check green",  "none"),
    ("results_four_fire",       42.1,  57.9, "The four that do fire",       "none"),
    ("results_impact",          57.9,  61.1, "None names the model",        "none"),
    ("terminal_run",            61.1,  71.2, "One model, one sentence",     "none"),
    ("datahub_model_features",  71.2,  80.8, "13 features in DataHub",      "slow_push"),
    ("datahub_lineage",         80.8,  87.4, "Column-level lineage",        "slow_push"),
    # The peak. One frame, held, deliberately still.
    ("datahub_model_properties", 87.4, 96.4, "Trained on vendors 1, 2, 6",  "none"),
    ("datahub_schema",          96.4, 104.9, "The unmapped column",         "slow_push"),
    ("readme_judges",          104.9, 116.8, "Neither side holds it",       "none"),
    ("datahub_incident",       116.8, 127.5, "Incident filed back",         "slow_push"),
    ("datahub_schema",         127.5, 134.8, "A note on the column",        "none"),
    ("pr_diff",                134.8, 137.8, "The fix it wrote",            "none"),
    ("rejected_patch",         137.8, 153.2, "87,693 rows destroyed",       "none"),
    ("pr_diff",                153.2, 160.8, "The patch it opened",         "none"),
    ("repo_home",              160.8, 169.9, "",                            "slow_push"),
]



def fix(word: str) -> str:
    stripped = word.strip()
    lead = word[: len(word) - len(word.lstrip())]
    trail = word[len(word.rstrip()):]
    key = stripped.lower().strip(".,!?")
    if key in FIXES:
        punct = "".join(c for c in stripped if c in ".,!?" and stripped.endswith(c))
        return f"{lead}{FIXES[key]}{punct}{trail}"
    return word


def main() -> None:
    CLIPS.mkdir(parents=True, exist_ok=True)
    needed = {s[0] for s in SEGMENTS}
    staged = 0
    for stem in sorted(needed):
        for d in SRC_DIRS:
            src = d / f"{stem}.png"
            if src.exists():
                shutil.copy2(src, CLIPS / f"{stem}.png")
                staged += 1
                break
        else:
            print(f"  MISSING CLIP: {stem}.png")
    print(f"staged {staged}/{len(needed)} clips into {CLIPS}")

    tr = json.loads((ROOT / "broll" / "transcript.json").read_text())
    words = tr.get("words") or []

    # render.py expects _transcript.segments[*].words[*], so nest the flat list.
    segments, fixed_count = [], 0
    for seg in tr.get("segments", []):
        seg_words = []
        for w in words:
            if seg["start"] - 0.01 <= w["start"] < seg["end"] + 0.01:
                corrected = fix(w["word"])
                if corrected != w["word"]:
                    fixed_count += 1
                seg_words.append(
                    {"word": corrected, "start": w["start"], "end": w["end"]}
                )
        text = " ".join(x["word"] for x in seg_words).strip() or seg["text"].strip()
        segments.append(
            {"start": seg["start"], "end": seg["end"], "text": text, "words": seg_words}
        )
    print(f"repaired {fixed_count} mis-transcribed words")

    plan = {
        "project_name": "Culprit",
        "width": 1920,
        "height": 1080,
        "add_intro_card": False,
        "theme": {
            "palette": {
                "bg": "#0B1020",
                "accent": "#F5A623",
                "text": "#F5F7FA",
                "text2": "#8A94A6",
            },
            "captions": {"margin_v": 110, "word_pop": False},
        },
        "segments": [
            {
                "clip_id": c,
                "start_time": round(s, 2),
                "end_time": round(e, 2),
                "lower_third": lt,
                "effect": fx,
            }
            for c, s, e, lt, fx in SEGMENTS
        ],
        "_transcript": {
            "text": " ".join(s["text"] for s in segments),
            "duration": tr.get("duration"),
            "segments": segments,
        },
    }
    PLAN.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    covered = SEGMENTS[-1][2] - SEGMENTS[0][1]
    print(f"wrote {PLAN.name}: {len(SEGMENTS)} segments covering {covered:.1f}s")


if __name__ == "__main__":
    main()
