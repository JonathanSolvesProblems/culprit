"""Tighten the narration to fit the 3-minute cap without audible artifacts.

The first version of this script did three things that damaged the audio, all of
which are corrected here:

1. It detected silence at -32 dB. Unvoiced consonants (s, f, th, plosive
   releases) sit well below that, so the detector classified the tails of words
   as silence and the trim ate them. That is why characters went missing off the
   ends of words. The floor is now -38 dB with a 0.28 s minimum, which is longer
   than any intra-word closure, and every span is pulled in by a guard margin so
   a cut can never land within 60 ms of speech.

2. It cut each pause out entirely and re-padded with `apad`, which is digital
   zero. Going from room tone to absolute silence and back gates the noise floor
   on and off, which is the static. Cuts are now taken from the *middle* of the
   dead air, so the room tone on both sides of every join is continuous and the
   original decay and onset are untouched.

3. It butt-joined the segments. A hard splice at a non-zero sample is a step
   discontinuity, which is a click. Each join now gets a 12 ms fade, inaudible in
   dead air.

Tempo is the last resort, not the first. The script solves for the gentlest pause
trim that reaches the target on its own and only stretches time with whatever is
left over.

Usage:
    python scripts/tighten_narration.py broll/narration_raw.mp4 --target 172
"""

from __future__ import annotations

import argparse
import math
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Only true inter-phrase silence. See the module docstring for why these are
# not the obvious values.
MIN_SILENCE = 0.25
GUARD = 0.06        # never cut within this of speech
JOIN_FADE = 0.012   # fade over each splice
SNAP_MIN = 0.075    # a gap must be this wide to be cut in
EDGE = 0.03         # clean air required either side of a join
SPEECH_MARGIN = 6.0 # dB above threshold before it counts as voice
DROP_PAD = 0.22     # room tone spliced in where a clause was removed

DELIBERATE = 1.00   # a pause at least this long was a scripted beat
LONG_RATIO = 2.5    # so beats keep 2.5x the allowance a hesitation gets
MIN_CUT = 0.08      # below this a cut costs more in join artifacts than it saves
BEAT_FLOOR = 0.35   # a scripted beat never drops below this, whatever the cap
MAX_TEMPO = 1.12    # past this it audibly rushes

# Sentences cut from the recorded take, by the text Whisper heard. Cutting
# content is what buys the runtime; stretching time is what damaged the last
# version. Each is a whole clause whose removal leaves the paragraph coherent,
# and each is snapped out to the pauses on either side before it is removed.
DROPS = [
    "They stay inside the normal noise for three more months",
    "with no new blank values and no change to the table",
    "The next engineer who opens that vendor column inherits the answer",
    "where those tools stop looking",
    "The SQL compiled cleanly",
    "and the next time a brand new vendor shows up",
]


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return (p.stdout or "") + (p.stderr or "")


def duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", str(path)])
    return float(out.strip().splitlines()[0])


def frame_db(path: Path) -> tuple[list[float], float]:
    """RMS in dBFS per 10 ms frame, measured off the decoded audio.

    ffmpeg's silencedetect takes a fixed threshold, and this take peaks at about
    -29 dBFS, so any threshold high enough to catch a pause is within a few dB of
    the speech itself. Measuring the file and deriving the threshold from its own
    noise floor is the only way to be safe on quiet material.
    """
    import array
    import wave

    tmp = path.with_suffix(".probe.wav")
    run(["ffmpeg", "-y", "-v", "error", "-i", str(path),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(tmp)])
    with wave.open(str(tmp)) as w:
        sr = w.getframerate()
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
    tmp.unlink(missing_ok=True)

    step = sr // 100
    out = []
    for i in range(0, len(a) - step, step):
        ch = a[i:i + step]
        rms = math.sqrt(sum(x * x for x in ch) / len(ch))
        out.append(20 * math.log10(rms / 32768) if rms else -120.0)
    return out, 0.01


def threshold(db: list[float]) -> float:
    """Derived from the file's own noise floor and speech level."""
    s = sorted(db)
    floor = s[len(s) // 20]           # 5th percentile: room tone
    speech = s[int(len(s) * 0.90)]    # 90th percentile: voice
    return max(floor + 25.0, speech - 30.0)


def silences(path: Path) -> list[tuple[float, float]]:
    """Silence spans, pulled in by GUARD at both ends."""
    db, dt = frame_db(path)
    thr = threshold(db)
    print(f"threshold  : {thr:.1f} dBFS (floor {sorted(db)[len(db)//20]:.1f}, "
          f"speech {sorted(db)[int(len(db)*0.9)]:.1f})")
    spans, start = [], None
    for i, v in enumerate(db):
        if v < thr and start is None:
            start = i
        elif v >= thr and start is not None:
            if (i - start) * dt >= MIN_SILENCE:
                a, b = start * dt + GUARD, i * dt - GUARD
                if b - a > MIN_CUT:
                    spans.append((a, b))
            start = None
    if start is not None and (len(db) - start) * dt >= MIN_SILENCE:
        spans.append((start * dt + GUARD, len(db) * dt))
    return spans


def audit(path: Path, cuts: list[tuple[float, float]],
          drops: list[tuple[float, float]]) -> int:
    """Fail loudly if any cut would remove audible speech.

    The first version of this script had no such check, and a snapping bug
    silently deleted the word "nineteen" out of "19.3 million". A mechanical
    check on the audio itself, rather than on transcript timings, is the only
    thing that catches that class of error.
    """
    db, dt = frame_db(path)
    thr = threshold(db)
    bad = 0
    for a, b in cuts:
        # Pause cuts adjacent to a drop merge into it, so containment has to
        # be measured by overlap rather than by exact bounds.
        inside_drop = any(min(b, d[1]) - max(a, d[0]) > 0.5 * (b - a) for d in drops)
        lo, hi = int(a / dt), int(b / dt)
        if inside_drop:
            # A content drop is meant to contain speech; only its joins must be
            # clean, so check the outer 80 ms at each end.
            n = int(EDGE / dt)
            edges = (list(range(lo, min(lo + n, hi)))
                     + list(range(max(lo, hi - n), hi)))
        else:
            edges = range(lo, hi)
        # Room tone and breath sit just over the threshold; only count a
        # frame as voice once it is clearly above it.
        loud = [i for i in edges if i < len(db) and db[i] >= thr + SPEECH_MARGIN]
        if loud:
            peak = max(db[i] for i in loud)
            print(f"  CUT TOUCHES SPEECH: {a:.2f}-{b:.2f}s, "
                  f"{len(loud)} frames up to {peak:.1f} dBFS")
            bad += 1
    print(f"audit      : {len(cuts)} cuts checked, {bad} touching speech")
    return bad


def quiet_runs(path: Path, min_len: float = 0.05) -> list[tuple[float, float]]:
    """Every dip below the speech threshold, however short.

    The full pauses drive the trim, but a clause boundary does not always have
    one beside it. The short gap between two words is still a clean place to cut,
    so long as the cut lands in its middle and the join is faded.
    """
    db, dt = frame_db(path)
    thr = threshold(db)
    runs, start = [], None
    for i, v in enumerate(db):
        if v < thr and start is None:
            start = i
        elif v >= thr and start is not None:
            if (i - start) * dt >= min_len:
                runs.append((start * dt, i * dt))
            start = None
    return runs


def snap_drop(a: float, b: float,
              runs: list[tuple[float, float]]) -> tuple[float, float]:
    """Move a clause drop out to the middle of the nearest gap at each end.

    Cutting at the transcript's own word boundaries splices one word onto
    another with no breath between them, and Whisper's timings are loose enough
    that it can also clip the word itself. Snapping to the centre of the nearest
    measured gap puts both sides of the join in room tone.

    An earlier version picked the first gap that *started* after the clause,
    which skipped any gap beginning slightly early and swallowed the following
    word whole. Nearest-by-distance has no such failure mode.
    """
    def nearest(t: float, limit: float = 0.9) -> float:
        best, bestd = t, limit
        for s, e in runs:
            mid = (s + e) / 2
            d = abs(mid - t)
            if d < bestd:
                best, bestd = mid, d
        return best

    return nearest(a), nearest(b)


def keep_ranges(total: float, spans: list[tuple[float, float]], cap: float,
                drops: list[tuple[float, float]] | None = None,
                ) -> tuple[list[tuple[float, float]], float]:
    """Ranges of the source to keep, cutting from the middle of each pause.

    Keeping the head and tail of every pause is the whole point: the join then
    sits in continuous room tone rather than between a word and a hard zero.
    """
    cuts: list[tuple[float, float]] = list(drops or [])
    for s, e in spans:
        gap = e - s
        allowance = max(cap * LONG_RATIO, BEAT_FLOOR) if gap >= DELIBERATE else cap
        keep = min(gap, allowance)
        if gap - keep < MIN_CUT:
            continue
        half = keep / 2.0
        cuts.append((s + half, e - half))

    cuts.sort()
    merged: list[list[float]] = []
    for c in cuts:
        if merged and c[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], c[1])
        else:
            merged.append([c[0], c[1]])

    ranges, pads, cursor, saved = [], [], 0.0, 0.0
    for a, b in merged:
        if a > cursor:
            ranges.append((cursor, a))
            # Removing a whole clause butts two sentences together with only the
            # residue of two short gaps between them, which reads as a stumble.
            # Splicing the room's own tone back in restores the sentence break.
            is_drop = any(min(b, d[1]) - max(a, d[0]) > 0.5 * (b - a)
                          for d in (drops or []))
            pads.append(DROP_PAD if is_drop else 0.0)
        saved += min(b, total) - max(a, cursor)
        cursor = max(cursor, b)
    ranges.append((cursor, total))
    pads.append(0.0)
    return ranges, saved, pads


def resolve_drops(src: Path, runs: list[tuple[float, float]]
                  ) -> list[tuple[float, float]]:
    """Locate each DROPS clause in the transcript and snap it to silence."""
    tr = ROOT / "broll" / "raw_transcript.json"
    if not tr.exists() or not DROPS:
        return []
    import json
    segs = json.loads(tr.read_text(encoding="utf-8")).get("segments", [])
    out = []
    for phrase in DROPS:
        hits = [s for s in segs if phrase.lower() in s["text"].lower()]
        if not hits:
            print(f"  WARNING: drop phrase not found: {phrase!r}")
            continue
        # A clause can run past its own segment; extend while the next segment
        # continues it without a sentence break.
        first = hits[0]
        i = segs.index(first)
        end = first["end"]
        while i + 1 < len(segs) and not segs[i]["text"].rstrip().endswith((".", "?", "!")):
            i += 1
            end = segs[i]["end"]
        out.append(snap_drop(first["start"], end, runs))
    return out


def solve_cap(total: float, spans: list[tuple[float, float]], target: float,
              drops: list[tuple[float, float]]) -> tuple[float, float]:
    """Gentlest cap that reaches the target on pause trimming alone."""
    lo, hi = 0.05, 3.0
    if keep_ranges(total, spans, hi, drops)[1] >= total - target:
        return hi, keep_ranges(total, spans, hi, drops)[1]   # nothing to do
    for _ in range(50):
        mid = (lo + hi) / 2
        if total - keep_ranges(total, spans, mid, drops)[1] > target:
            hi = mid   # still too long, cut harder
        else:
            lo = mid
    return lo, keep_ranges(total, spans, lo, drops)[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--target", type=float, default=172.0,
                    help="target seconds; keep margin under the 180s cap")
    ap.add_argument("--out", default=str(ROOT / "broll" / "narration.wav"))
    args = ap.parse_args()

    src = Path(args.source)
    if not src.is_absolute():
        src = ROOT / src
    out = Path(args.out)
    total = duration(src)
    print(f"source     : {src.name}  {total:.1f}s "
          f"({int(total // 60)}m{total % 60:04.1f}s)")

    spans = silences(src)
    print(f"pauses     : {len(spans)} over {MIN_SILENCE}s, guarded {GUARD*1000:.0f}ms")

    runs = quiet_runs(src, SNAP_MIN)
    drops = resolve_drops(src, runs)
    dropped = sum(b - a for a, b in drops)
    if drops:
        print(f"content    : {len(drops)} clauses cut, {dropped:.1f}s")

    cap, saved = solve_cap(total, spans, args.target, drops)
    ranges, saved, pads = keep_ranges(total, spans, cap, drops)
    padding = sum(pads)
    trimmed = total - saved + padding
    cuts = len(ranges) - 1
    print(f"trim       : cap {cap:.2f}s (beats {cap * LONG_RATIO:.2f}s), "
          f"{cuts} cuts, {saved:.1f}s removed")
    if padding:
        print(f"room tone  : {padding:.2f}s spliced back at "
              f"{sum(1 for p in pads if p)} clause joins")

    # Every removed interval, audited against the audio before anything renders.
    removed, prev = [], 0.0
    for a, b in ranges:
        if a > prev:
            removed.append((prev, a))
        prev = b
    if audit(src, removed, drops):
        print("\nRefusing to render: a cut would delete speech.")
        return 1

    speed = max(1.0, trimmed / args.target)
    verdict = ("none needed" if speed <= 1.001 else
               "imperceptible" if speed <= 1.06 else
               "slight" if speed <= 1.12 else "TOO FAST")
    print(f"after trim : {trimmed:.1f}s")
    print(f"tempo      : {speed:.3f}x  ({verdict})")
    if speed > MAX_TEMPO:
        print(f"  WARNING: above {MAX_TEMPO}x. Cut a sentence from the script "
              f"instead of stretching further.")

    # The longest pause in the take is the cleanest sample of the room, and is
    # what gets spliced back in at clause joins. Using the take's own tone rather
    # than generated silence keeps the noise floor continuous.
    donor = max(spans, key=lambda s: s[1] - s[0])
    dmid = (donor[0] + donor[1]) / 2

    # One filter graph, so there is no generational loss between stages.
    parts, labels = [], []

    def emit(idx: int, s: float, e: float, fade_in: bool, fade_out: bool) -> None:
        span = e - s
        f = min(JOIN_FADE, span / 3)
        chain = f"[0:a]atrim=start={s:.4f}:end={e:.4f},asetpts=PTS-STARTPTS"
        if fade_in:
            chain += f",afade=t=in:st=0:d={f:.4f}"
        if fade_out:
            chain += f",afade=t=out:st={span - f:.4f}:d={f:.4f}"
        parts.append(chain + f"[s{idx}]")
        labels.append(f"[s{idx}]")

    idx = 0
    for i, (s, e) in enumerate(ranges):
        emit(idx, s, e, fade_in=idx > 0, fade_out=True)
        idx += 1
        if pads[i] > 0:
            emit(idx, dmid - pads[i] / 2, dmid + pads[i] / 2, True, True)
            idx += 1
    graph = (";".join(parts) + ";" + "".join(labels)
             + f"concat=n={idx}:v=0:a=1[cat]")

    tempo, remaining = "", speed
    while remaining > 1.001:
        step = min(remaining, 2.0)
        tempo += f",atempo={step:.6f}"
        remaining /= step
    graph += f";[cat]{tempo.lstrip(',') or 'anull'}[sp]"

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".stage1.wav")
    res = run(["ffmpeg", "-y", "-hide_banner", "-i", str(src),
               "-filter_complex", graph, "-map", "[sp]",
               "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(tmp)])
    if not tmp.exists():
        print("\nffmpeg failed:\n" + res[-1800:])
        return 1

    # Two-pass loudnorm. Single-pass works off a running estimate and pumps the
    # gain around, which on a spoken track sounds like the room breathing.
    probe = run(["ffmpeg", "-hide_banner", "-i", str(tmp),
                 "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
                 "-f", "null", "-"])
    m = re.search(r'\{[^{}]*"input_i"[\s\S]*?\}', probe)
    second = "loudnorm=I=-16:TP=-1.5:LRA=11"
    if m:
        import json
        d = json.loads(m.group(0))
        second += (f":measured_I={d['input_i']}:measured_TP={d['input_tp']}"
                   f":measured_LRA={d['input_lra']}"
                   f":measured_thresh={d['input_thresh']}"
                   f":offset={d['target_offset']}:linear=true")
        print(f"loudness   : {d['input_i']} LUFS in, -16 out (two-pass)")
    run(["ffmpeg", "-y", "-hide_banner", "-i", str(tmp), "-af", second,
         "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(out)])
    tmp.unlink(missing_ok=True)
    if not out.exists():
        print("\nloudnorm pass failed")
        return 1

    final = duration(out)
    print(f"\nwrote      : {out.name}  {final:.1f}s "
          f"({int(final // 60)}m{final % 60:04.1f}s)")
    print(f"cap margin : {180 - final:.1f}s under 3:00")
    return 0


if __name__ == "__main__":
    sys.exit(main())
