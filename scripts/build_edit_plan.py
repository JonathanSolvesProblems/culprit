"""Stage b-roll and author the vidkit edit plan for the Culprit demo.

vidkit's planner needs an Anthropic key, which this project does not use. The
mapping is authored here instead, which is arguably better anyway: the narration
was written before the b-roll was captured, so which frame belongs under which
sentence is already known and does not need inferring.

Frames are keyed to a phrase in the narration, not to a timestamp. Timestamps
went stale every time the audio was re-tightened, and a b-roll frame sitting
under the wrong sentence is worse than no b-roll at all. Anchors re-resolve
themselves against whatever transcript is current.

Also repairs the transcript. Whisper mangles the project name and several
technical terms, and it emits numbers as bare digit tokens, so "$1.37" arrives
as "1" followed by "37". Captions are burned in, so all of that has to be fixed
before rendering.

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
TRANSCRIPT = ROOT / "broll" / "transcript.json"

# Consecutive tokens Whisper splits that have to be rejoined for captions.
# Longest first, so "19 3 million" is not eaten by "3 5 million".
MERGES: list[tuple[tuple[str, ...], str]] = [
    (("19", "3", "million"), "19.3 million"),
    (("3", "5", "million"), "3.5 million"),
    (("1", "37"), "$1.37"),
    (("66", "000"), "66,000"),
    (("90", "000"), "$90,000"),
    (("87", "000"), "87,000"),
    (("catch", "all"), "catch-all"),
]

# Whole-word substitutions, matched case-insensitively.
FIXES = {
    "culprits": "Culprit",
    "fair": "fare",
    "taxes": "taxis",
    "churns": "turns",
}

# Fixes that are only correct in one place. "and" is a real word everywhere
# else in the script, so it can only be repaired where the context identifies it.
CONTEXT_FIXES = [("arrived on time", "and", "in")]

# (clip stem, anchor phrase in the narration, lower third, effect)
# Each frame runs until the next anchor. Motion is off on the text-dense frames:
# zoompan steps in whole pixels and makes small type shimmer.
ANCHORS = [
    ("results_semantic_change",  "In December 2024",           "The change, in the real feed", "none"),
    ("semantic_change_json",     "The vendor ID column",       "Detected, not planted",        "none"),
    ("results_impact",           "That threw off",             "$1.37 on every trip",          "none"),
    ("results_vendors",          "not an estimate",            "Measured against a control",   "none"),
    ("results_monitors",         "Every standard data",        "Every standard check green",   "none"),
    ("results_four_fire",        "Four monitor alerts",        "Two are false alarms",         "none"),
    ("results_monitors",         "The two real ones",          "255 rows in 3.5 million",      "none"),
    ("results_four_fire",        "Not one of those",           "None names the model",         "none"),
    ("terminal_run",             "The tool I built",           "One model, one sentence",      "none"),
    ("datahub_model_features",   "On its own",                 "13 features in DataHub",       "slow_push"),
    ("datahub_lineage",          "then it confirms",           "Column-level lineage",         "slow_push"),
    # The peak. One frame, held, deliberately still.
    ("datahub_model_properties", "metadata graph",             "Trained on vendors 1, 2, 6",   "none"),
    ("datahub_schema",           "The warehouse, meanwhile",   "Serving vendor 7",             "none"),
    ("datahub_model_features",   "The model's encoder",        "No slot for vendor 7",         "none"),
    ("readme_judges",            "A model monitoring tool",    "Neither side holds it",        "none"),
    ("datahub_incident",         "writes the answer back",     "Incident filed back",          "slow_push"),
    ("datahub_schema",           "writes the investigation",   "A note on the column",         "none"),
    ("rejected_patch",           "first generated patch",      "87,000 rows destroyed",        "none"),
    ("pr_diff",                  "finally opened",             "The patch it opened",          "none"),
    ("repo_home",                "19.3 million real",          "",                             "slow_push"),
]


def repair(words: list[dict]) -> tuple[list[dict], int]:
    """Merge split numbers, then substitute mis-heard words."""
    out, i, n = [], 0, 0
    while i < len(words):
        for pattern, joined in MERGES:
            k = len(pattern)
            got = tuple(words[i + j]["word"].strip().lower()
                        for j in range(k)) if i + k <= len(words) else ()
            if got == pattern:
                out.append({"word": joined, "start": words[i]["start"],
                            "end": words[i + k - 1]["end"]})
                i += k
                n += 1
                break
        else:
            w = dict(words[i])
            key = w["word"].strip().lower().strip(".,!?")
            if key in FIXES:
                w["word"] = FIXES[key]
                n += 1
            out.append(w)
            i += 1
    return out, n


def anchor_time(segments: list[dict], phrase: str) -> float:
    """Match on the raw text, not the repaired text.

    Rebuilding a segment's text from Whisper's word tokens drops the
    punctuation, so an anchor written the way the sentence actually reads would
    silently stop matching.
    """
    for s in segments:
        if phrase.lower() in s.get("raw", s["text"]).lower():
            return float(s["start"])
    raise SystemExit(f"anchor phrase not found in the narration: {phrase!r}")


def main() -> None:
    tr = json.loads(TRANSCRIPT.read_text(encoding="utf-8"))
    duration = float(tr.get("duration"))
    words, fixed = repair(tr.get("words") or [])

    # render.py expects _transcript.segments[*].words[*], so nest the flat list.
    segments = []
    for seg in tr.get("segments", []):
        sw = [w for w in words
              if seg["start"] - 0.01 <= w["start"] < seg["end"] + 0.01]
        for context, wrong, right in CONTEXT_FIXES:
            if context.lower() in seg["text"].lower():
                for w in sw:
                    if w["word"].strip().lower() == wrong:
                        w["word"] = right
                        fixed += 1
                        break
        text = " ".join(w["word"] for w in sw).strip() or seg["text"].strip()
        segments.append({"start": seg["start"], "end": seg["end"],
                         "text": text, "words": sw, "raw": seg["text"]})
    print(f"repaired {fixed} transcript tokens")

    starts = [anchor_time(segments, a[1]) for a in ANCHORS]
    if starts != sorted(starts):
        raise SystemExit("anchors resolved out of order; check ANCHORS")
    ends = starts[1:] + [duration]

    CLIPS.mkdir(parents=True, exist_ok=True)
    staged = 0
    for stem in sorted({a[0] for a in ANCHORS}):
        for d in SRC_DIRS:
            if (d / f"{stem}.png").exists():
                shutil.copy2(d / f"{stem}.png", CLIPS / f"{stem}.png")
                staged += 1
                break
        else:
            raise SystemExit(f"missing clip: {stem}.png")
    print(f"staged {staged} clips into {CLIPS}")

    plan = {
        "project_name": "Culprit",
        "width": 1920,
        "height": 1080,
        "add_intro_card": False,
        "theme": {
            "palette": {"bg": "#0B1020", "accent": "#F5A623",
                        "text": "#F5F7FA", "text2": "#8A94A6"},
            "captions": {"margin_v": 110, "word_pop": False},
        },
        "segments": [
            {"clip_id": c, "start_time": round(s, 2), "end_time": round(e, 2),
             "lower_third": lt, "effect": fx}
            for (c, _, lt, fx), s, e in zip(ANCHORS, starts, ends)
        ],
        "_transcript": {
            "text": " ".join(s["text"] for s in segments),
            "duration": duration,
            "segments": segments,
        },
    }
    PLAN.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    for i, (seg, (c, _, _, fx)) in enumerate(zip(plan["segments"], ANCHORS)):
        dup = " <-- REPEATS PREVIOUS" if i and c == ANCHORS[i - 1][0] else ""
        print(f"  {seg['start_time']:6.1f}-{seg['end_time']:6.1f}  "
              f"{c:26s} {fx:10s}{dup}")
    print(f"wrote {PLAN.name}: {len(ANCHORS)} segments covering {duration:.1f}s")


if __name__ == "__main__":
    main()
