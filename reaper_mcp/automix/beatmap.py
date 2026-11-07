"""Beat data for a song, read from files the sibling analysers already wrote.

This module never analyses audio. Beats come from:

1. ``<dir>/.automixer_cache/<stem>_<sha1 of resolved path>[:10].beatmap.json``
   (``automixer_mcp``, transient-snapped)                -> provenance "snapped"
2. ``<dir>/beat_this/<stem>.beats`` or ``<dir>/<stem>.beats`` (``conga_automixer``
   ``automixer beatthis``; ``<time>\\t<beat_number>`` per line), with the
   ``<stem>.bars.json`` next to it saying whether beats were aligned
                                                         -> "aligned" / "unaligned" / "unknown"
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from reaper_mcp_shared.error_codes import ErrorCode, ReaperMCPError

# A beat interval inside a clean span may differ from the span's median by this factor.
INTERVAL_MIN_RATIO = 0.67
INTERVAL_MAX_RATIO = 1.5

_CACHE_HASH = re.compile(r"^[0-9a-f]{10}$")


@dataclass
class Beatmap:
    beats: list[float]
    beat_numbers: list[int]
    downbeat_idx: list[int]          # indices into `beats` where beat_number == 1
    provenance: str                  # snapped | aligned | unaligned | unknown
    source: Path
    audio: str = ""                  # the audio file this belongs to
    warnings: list[str] = field(default_factory=list)

    @property
    def bar_count(self) -> int:
        return len(self.downbeat_idx)


def song_name(audio_path: str | Path) -> str:
    return Path(audio_path).stem


def _no_beats_error(audio: Path) -> ReaperMCPError:
    return ReaperMCPError(
        ErrorCode.INVALID_PARAMETER,
        f"No beats for {audio}: run `automixer beatthis {audio.parent}`",
    )


def _build(beats: list[float], numbers: list[int], provenance: str, source: Path, audio: Path) -> Beatmap:
    if len(beats) < 2 or len(beats) != len(numbers):
        raise ReaperMCPError(
            ErrorCode.INVALID_PARAMETER,
            f"Unusable beat data in {source}: {len(beats)} beats, {len(numbers)} beat numbers",
        )
    if not all(math.isfinite(t) for t in beats) or any(b <= a for a, b in zip(beats, beats[1:])):
        raise ReaperMCPError(ErrorCode.INVALID_PARAMETER, f"Beat times in {source} are not strictly increasing")
    downbeats = [i for i, n in enumerate(numbers) if n == 1]
    if not downbeats:
        raise ReaperMCPError(ErrorCode.INVALID_PARAMETER, f"No downbeats (beat number 1) in {source}")
    bm = Beatmap(beats, numbers, downbeats, provenance, source, audio=str(audio))
    if provenance in ("unaligned", "unknown"):
        bm.warnings.append(
            f"beats for {song_name(audio)} may be up to 20 ms off ({provenance}); verify will measure it"
        )
    return bm


def _read_json_beatmap(path: Path, audio: Path) -> Beatmap:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))["beatmap"]
        beats = [float(x) for x in data["beats"]]
        numbers = [int(x) for x in data["beat_numbers"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReaperMCPError(ErrorCode.INVALID_PARAMETER, f"Cannot read beatmap {path}: {exc}") from exc
    return _build(beats, numbers, "snapped", path, audio)


def _read_beats_file(path: Path, audio: Path) -> Beatmap:
    beats: list[float] = []
    numbers: list[int] = []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReaperMCPError(ErrorCode.INVALID_PARAMETER, f"Cannot read beats file {path}: {exc}") from exc
    for lineno, line in enumerate(text.splitlines(), 1):
        parts = line.split()
        if not parts:
            continue
        try:
            beats.append(float(parts[0]))
            numbers.append(int(float(parts[1])) if len(parts) > 1 else (len(numbers) % 4) + 1)
        except ValueError as exc:
            raise ReaperMCPError(
                ErrorCode.INVALID_PARAMETER, f"{path} line {lineno}: expected '<seconds> <beat number>'"
            ) from exc
    aligned = None
    bars_json = path.with_name(path.stem + ".bars.json")
    if bars_json.is_file():
        try:
            flag = json.loads(bars_json.read_text(encoding="utf-8")).get("aligned")
            aligned = flag if isinstance(flag, bool) else None
        except (OSError, ValueError, AttributeError):
            aligned = None
    provenance = "unknown" if aligned is None else ("aligned" if aligned else "unaligned")
    return _build(beats, numbers, provenance, path, audio)


def _find_snapped(audio: Path) -> Path | None:
    cache = audio.parent / ".automixer_cache"
    if not cache.is_dir():
        return None
    digest = hashlib.sha1(str(audio.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    exact = cache / f"{audio.stem}_{digest}.beatmap.json"
    if exact.is_file():
        return exact
    prefix, suffix = f"{audio.stem}_", ".beatmap.json"
    others = sorted(
        p for p in cache.iterdir()
        if p.is_file() and p.name.startswith(prefix) and p.name.endswith(suffix)
        and _CACHE_HASH.match(p.name[len(prefix):-len(suffix)])
    )
    return others[0] if len(others) == 1 else None


def load_beatmap(audio_path: str | Path) -> Beatmap:
    """Beat data for `audio_path`, first hit wins; see the module docstring."""
    audio = Path(audio_path)
    snapped = _find_snapped(audio)
    if snapped is not None:
        return _read_json_beatmap(snapped, audio)
    for candidate in (audio.parent / "beat_this" / f"{audio.stem}.beats", audio.parent / f"{audio.stem}.beats"):
        if candidate.is_file():
            return _read_beats_file(candidate, audio)
    raise _no_beats_error(audio)


def clean_span(bm: Beatmap, first_bar: int, n_bars: int) -> list[str]:
    """Problems with the `n_bars` bars starting at 1-based `first_bar`; empty means clean.

    Clean = the 4*n_bars + 1 beats up to and including the next downbeat exist,
    are numbered 1-2-3-4 with no gap, and every interval is within 0.67-1.5x
    the span's median interval.
    """
    if first_bar < 1 or n_bars < 1:
        return [f"invalid span (bar {first_bar}, {n_bars} bars)"]
    if first_bar > bm.bar_count:
        return [f"bar {first_bar} does not exist (song has {bm.bar_count} bars)"]
    i0 = bm.downbeat_idx[first_bar - 1]
    end = i0 + 4 * n_bars
    if end >= len(bm.beats):
        return [f"bars {first_bar}-{first_bar + n_bars - 1} run past the last beat"]
    problems = []
    for k in range(4 * n_bars + 1):
        if bm.beat_numbers[i0 + k] != (k % 4) + 1:
            problems.append(f"beat {i0 + k} is numbered {bm.beat_numbers[i0 + k]}, expected {(k % 4) + 1}")
            break
    intervals = [bm.beats[i + 1] - bm.beats[i] for i in range(i0, end)]
    median = statistics.median(intervals)
    for k, iv in enumerate(intervals):
        if not INTERVAL_MIN_RATIO * median <= iv <= INTERVAL_MAX_RATIO * median:
            problems.append(f"interval {k} is {iv:.3f}s against a median of {median:.3f}s")
            break
    return problems


def first_clean_bar(bm: Beatmap, n_bars: int) -> int | None:
    """The first 1-based bar where `n_bars` bars are clean, or None."""
    last_start = bm.bar_count - n_bars + 1
    for bar in range(1, last_start + 1):
        if not clean_span(bm, bar, n_bars):
            return bar
    return None


def longest_clean_run(bm: Beatmap, limit: int = 64) -> int:
    """The most bars (up to `limit`) that are clean anywhere in the song."""
    for n in range(min(limit, bm.bar_count), 0, -1):
        if first_clean_bar(bm, n) is not None:
            return n
    return 0
