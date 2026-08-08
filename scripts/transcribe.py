"""Word-level transcript of a narration take, via the OpenAI Whisper API.

Word timestamps are what let the edit plan anchor each b-roll frame to the
sentence that describes it, and what drives the burned-in captions. Local
faster-whisper would work too; this project already has an OpenAI key and no
Anthropic one, so it uses what is here.

Usage:  python scripts/transcribe.py broll/narration_raw.mp4 --out broll/raw_transcript.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--out", default=str(ROOT / "broll" / "raw_transcript.json"))
    args = ap.parse_args()

    src = Path(args.source)
    if not src.is_absolute():
        src = ROOT / src

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    with src.open("rb") as fh:
        r = client.audio.transcriptions.create(
            model="whisper-1", file=fh, response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
        )
    data = r.model_dump()
    out = Path(args.out)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"{out.name}: {data.get('duration'):.1f}s, "
          f"{len(data.get('words') or [])} words, "
          f"{len(data.get('segments') or [])} segments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
