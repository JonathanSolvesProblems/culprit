"""Tighten the narration to fit the 3-minute cap without sounding rushed.

Two passes, in this order, because they are not equivalent:

1. Differential silence trimming. Short hesitations between phrases carry no
   meaning and are cut hard. The long pauses are deliberate beats the script
   asked for, so they are shortened but preserved. A blanket cap would flatten
   both and make the delivery sound breathless.
2. A modest tempo change on whatever gap remains. Under about 1.12x this is
   imperceptible on speech; past 1.20x it audibly rushes.

Finally normalises loudness to EBU R128 so the voice sits at a consistent level.

Usage:
    python scripts/tighten_narration.py broll/<recording>.mp4 --target 172
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Pauses shorter than this are hesitations, and get cut to SHORT_CAP.
DELIBERATE = 1.00
SHORT_CAP = 0.22   # between phrases
LONG_CAP = 0.65    # kept for the scripted beats
NOISE_DB = -32
MIN_SILENCE = 0.18


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return (p.stdout or "") + (p.stderr or "")


def duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)])
    return float(out.strip().splitlines()[0])


def silences(path: Path) -> list[tuple[float, float]]:
    out = run(["ffmpeg", "-hide_banner", "-i", str(path),
               "-af", f"silencedetect=noise={NOISE_DB}dB:d={MIN_SILENCE}", "-f", "null", "-"])
    spans, start = [], None
    for line in out.splitlines():
        s = re.search(r"silence_start: (-?[\d.]+)", line)
        e = re.search(r"silence_end: ([\d.]+)", line)
        if s:
            start = float(s.group(1))
        elif e and start is not None:
            spans.append((max(0.0, start), float(e.group(1))))
            start = None
    return spans


def keep_segments(total: float, spans: list[tuple[float, float]]) -> tuple[list, float]:
    """Speech segments to keep, plus the padding to reinsert after each."""
    segs, cursor, saved = [], 0.0, 0.0
    for s, e in spans:
        gap = e - s
        if s > cursor:
            cap = LONG_CAP if gap >= DELIBERATE else SHORT_CAP
            keep = min(gap, cap)
            saved += max(0.0, gap - keep)
            segs.append((cursor, s, keep))
        cursor = e
    if cursor < total:
        segs.append((cursor, total, 0.0))
    return segs, saved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--target", type=float, default=172.0,
                    help="target seconds; keep margin under the 180s cap")
    ap.add_argument("--out", default=str(ROOT / "broll" / "narration.wav"))
    ap.add_argument("--short-cap", type=float, default=SHORT_CAP)
    ap.add_argument("--long-cap", type=float, default=LONG_CAP)
    args = ap.parse_args()

    globals()["SHORT_CAP"] = args.short_cap
    globals()["LONG_CAP"] = args.long_cap
    src = Path(args.source)
    if not src.is_absolute():
        src = ROOT / src
    out = Path(args.out)
    total = duration(src)
    print(f"source     : {src.name}  {total:.1f}s ({int(total//60)}m{total%60:04.1f}s)")

    spans = silences(src)
    segs, saved = keep_segments(total, spans)
    print(f"pauses     : {len(spans)} detected, {saved:.1f}s trimmed "
          f"(hesitations -> {SHORT_CAP}s, beats -> {LONG_CAP}s)")

    trimmed = total - saved
    speed = max(1.0, trimmed / args.target)
    verdict = ("imperceptible" if speed <= 1.08 else
               "slight" if speed <= 1.14 else
               "noticeable" if speed <= 1.20 else "TOO FAST")
    print(f"after trim : {trimmed:.1f}s")
    print(f"tempo      : {speed:.3f}x  ({verdict})")

    # Build one filter graph: cut each speech segment, pad it, concat, retime,
    # then normalise. Doing it in a single pass avoids generational loss.
    parts, labels = [], []
    for i, (s, e, pad) in enumerate(segs):
        parts.append(
            f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS"
            + (f",apad=pad_dur={pad:.3f}" if pad > 0.001 else "")
            + f"[s{i}]"
        )
        labels.append(f"[s{i}]")
    graph = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(segs)}:v=0:a=1[cat]"

    tempo = ""
    remaining = speed
    while remaining > 1.001:
        step = min(remaining, 2.0)
        tempo += f",atempo={step:.6f}"
        remaining /= step
    graph += f";[cat]{tempo.lstrip(',') or 'anull'}[sp]"
    graph += ";[sp]loudnorm=I=-16:TP=-1.5:LRA=11[out]"

    out.parent.mkdir(parents=True, exist_ok=True)
    res = run(["ffmpeg", "-y", "-hide_banner", "-i", str(src),
               "-filter_complex", graph, "-map", "[out]",
               "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(out)])
    if not out.exists():
        print("\nffmpeg failed:\n" + res[-1800:])
        return 1

    final = duration(out)
    print(f"\nwrote      : {out.name}  {final:.1f}s ({int(final//60)}m{final%60:04.1f}s)")
    print(f"cap margin : {180 - final:.1f}s under 3:00")
    return 0


if __name__ == "__main__":
    sys.exit(main())
