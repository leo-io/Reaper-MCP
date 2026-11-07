"""Lay out an ordered list of songs as a two-deck preview minimix.

Pure arithmetic, no I/O. The layout is fixed in v1:

    song 1:  [ solo 4 ][ transition T ]
    song 2:            [ transition T ][ solo 4 ][ transition T ]
    song 3:                                      [ transition T ][ solo 4 ]

Songs keep the order they are given in. A transition is 16 bars and shrinks
to 8 or 4 only for the pair whose songs have no clean stretch long enough.

Timeline facts (measured in REAPER 7.79, see docs/superpowers/plans):
* a linear tempo ramp from b0 to b1 BPM over N bars takes
  T = 4N * 60 / ((b0 + b1) / 2) seconds and puts beat k at
  t_k = (-b0 + sqrt(b0^2 + 120 * s * k)) / s, with s = (b1 - b0) / T;
* per-beat stretch markers (SM) pin each song's source beats to those times;
* solo bars are not stretched, so the tempo map holds 960 / D BPM across them
  (D = source seconds of the four bars) which makes the grid bar-exact there.

One deliberate detail: the point that ends a ramp is also the point that
starts the next solo, so it can carry only one tempo. A ramp therefore ends on
the incoming song's *solo* tempo (the mean over its four solo bars, which
follow the transition immediately) rather than a separate mean over its
transition beats. Starting tempo is the mean over the outgoing transition's
beats, as planned.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from reaper_mcp_shared.error_codes import ErrorCode, ReaperMCPError

from .beatmap import Beatmap, first_clean_bar, longest_clean_run, song_name
from .rpp_quote import clean_label

TRANSITION_BARS = 16
TRANSITION_SHRINK = (16, 8, 4)
SOLO_BARS = 4
FADE_SHAPE = 1
BEATS_PER_BAR = 4
MAX_TEMPO_JUMP_ERROR = 0.16
MAX_TEMPO_JUMP_WARN = 0.06
PHASE_FRACTIONS = (1 / 8, 3 / 8, 5 / 8, 7 / 8)


@dataclass
class TempoPoint:
    time: float
    bpm: float
    shape: int          # 0 = linear ramp to the next point, 1 = square


@dataclass
class ItemPlan:
    order: int                      # 1-based position in the caller's list
    deck: str                       # "A" or "B"
    name: str
    audio: str
    provenance: str
    beat_source: str                # the beat file the layout was built from
    in_bar: int
    bpm: float                      # the song's solo tempo
    position: float                 # project seconds
    soffs: float                    # source seconds at `position`
    length: float
    sms: list[tuple[float, float]]  # (take position, absolute source time)
    fade_in: float
    fade_out: float


@dataclass
class TransitionPlan:
    from_order: int
    to_order: int
    bars: int
    bpm_start: float
    bpm_end: float
    stretch_pct: float
    start_time: float
    end_time: float
    start_qn: float


@dataclass
class Plan:
    items: list[ItemPlan]
    tempo_points: list[TempoPoint]
    transitions: list[TransitionPlan]
    probe_points: list[dict]        # {item, sm_index, qn, time}: every transition SM
    phase_points: list[dict]        # {transition, beat, time}: 4 per transition
    warnings: list[str] = field(default_factory=list)


def ramp_beat_times(n_bars: int, bpm_start: float, bpm_end: float) -> list[float]:
    """Seconds from the ramp's start to each of its 4*n_bars + 1 beats."""
    beats = n_bars * BEATS_PER_BAR
    duration = beats * 60.0 / ((bpm_start + bpm_end) / 2.0)
    slope = (bpm_end - bpm_start) / duration
    if abs(slope) < 1e-12:
        return [k * 60.0 / bpm_start for k in range(beats + 1)]
    times = [(-bpm_start + math.sqrt(bpm_start ** 2 + 120.0 * slope * k)) / slope for k in range(beats + 1)]
    times[0] = 0.0
    times[-1] = duration
    return times


def _mean_bpm(bm: Beatmap, first_beat: int, n_beats: int) -> float:
    return 60.0 * n_beats / (bm.beats[first_beat + n_beats] - bm.beats[first_beat])


def _spans(n_songs: int, bars: list[int]) -> list[int]:
    """Bars each song needs: incoming transition + solo + outgoing transition."""
    return [
        (bars[i - 1] if i > 0 else 0) + SOLO_BARS + (bars[i] if i < n_songs - 1 else 0)
        for i in range(n_songs)
    ]


def _choose_bars_and_entry(maps: list[Beatmap]) -> tuple[list[int], list[int]]:
    """Transition length per pair and each song's entry bar. Shrinks only where needed."""
    n = len(maps)
    bars = [TRANSITION_BARS] * (n - 1)
    while True:
        spans = _spans(n, bars)
        entries = [first_clean_bar(bm, spans[i]) for i, bm in enumerate(maps)]
        bad = next((i for i, e in enumerate(entries) if e is None), None)
        if bad is None:
            return bars, entries  # type: ignore[return-value]
        adjacent = [j for j in (bad - 1, bad) if 0 <= j < n - 1]
        shrinkable = [j for j in adjacent if bars[j] > TRANSITION_SHRINK[-1]]
        if not shrinkable:
            name = song_name(maps[bad].audio)
            raise ReaperMCPError(
                ErrorCode.INVALID_PARAMETER,
                f"Song {bad + 1} ({name}) has no clean stretch of {spans[bad]} bars even with "
                f"{TRANSITION_SHRINK[-1]}-bar transitions; its longest clean run is "
                f"{longest_clean_run(maps[bad])} bars",
            )
        # halve the larger adjacent transition; on a tie, the earlier pair
        target = max(shrinkable, key=lambda j: (bars[j], -j))
        bars[target] = TRANSITION_SHRINK[TRANSITION_SHRINK.index(bars[target]) + 1]


def plan(maps: list[Beatmap]) -> Plan:
    """Build the layout for `maps` (one per song, in the caller's order)."""
    n = len(maps)
    if n < 2:
        raise ReaperMCPError(ErrorCode.INVALID_PARAMETER, "A minimix needs at least 2 songs")
    names = [clean_label(song_name(bm.audio)) or f"song {i + 1}" for i, bm in enumerate(maps)]
    warnings = [w for bm in maps for w in bm.warnings]
    bars, entries = _choose_bars_and_entry(maps)

    # Per song: first beat index of each section and the solo's source span.
    solo_first = []     # beat index where the solo starts
    solo_tempo = []     # BPM that makes the solo bar-exact on the grid
    solo_secs = []
    for i, bm in enumerate(maps):
        i0 = bm.downbeat_idx[entries[i] - 1]
        t_in = bars[i - 1] if i > 0 else 0
        s0 = i0 + t_in * BEATS_PER_BAR
        s1 = s0 + SOLO_BARS * BEATS_PER_BAR
        solo_first.append(s0)
        solo_secs.append(bm.beats[s1] - bm.beats[s0])
        solo_tempo.append(SOLO_BARS * BEATS_PER_BAR * 60.0 / solo_secs[-1])

    # Tempo per pair, with the jump checks.
    pair_bpm = []
    for j in range(n - 1):
        s_out = solo_first[j] + SOLO_BARS * BEATS_PER_BAR
        bpm_start = _mean_bpm(maps[j], s_out, bars[j] * BEATS_PER_BAR)
        bpm_end = solo_tempo[j + 1]
        jump = abs(bpm_end - bpm_start) / bpm_start
        if jump > MAX_TEMPO_JUMP_ERROR:
            raise ReaperMCPError(
                ErrorCode.INVALID_PARAMETER,
                f"Tempo jump too large between song {j + 1} ({names[j]}, {bpm_start:.1f} BPM) and song "
                f"{j + 2} ({names[j + 1]}, {bpm_end:.1f} BPM): {jump * 100:.1f}% (limit "
                f"{MAX_TEMPO_JUMP_ERROR * 100:.0f}%)",
            )
        if jump > MAX_TEMPO_JUMP_WARN:
            warnings.append(
                f"Tempo jump of {jump * 100:.1f}% between song {j + 1} ({names[j]}) and song "
                f"{j + 2} ({names[j + 1]}): expect audible stretching"
            )
        pair_bpm.append((bpm_start, bpm_end))

    transitions: list[TransitionPlan] = []
    tempo_points = [TempoPoint(0.0, solo_tempo[0], 1)]
    probe_points: list[dict] = []
    phase_points: list[dict] = []
    sms: list[list[tuple[float, float]]] = [[] for _ in range(n)]
    item_pos = [0.0] * n        # project seconds where each song's item starts
    ramp_secs = [0.0] * n       # length of the transition INTO each song

    qn = 0.0
    for i, bm in enumerate(maps):
        # Take positions are item-relative: a song's item starts with its
        # incoming transition (or, for the first song, with its solo).
        take = ramp_secs[i]
        if i == 0:
            sms[i].append((0.0, bm.beats[solo_first[i]]))
        solo_end_beat = solo_first[i] + SOLO_BARS * BEATS_PER_BAR
        take += solo_secs[i]
        qn += SOLO_BARS * BEATS_PER_BAR
        if i == n - 1:
            sms[i].append((take, bm.beats[solo_end_beat]))
            break
        # Transition i -> i+1
        n_bars = bars[i]
        n_beats = n_bars * BEATS_PER_BAR
        bpm_start, bpm_end = pair_bpm[i]
        rel = ramp_beat_times(n_bars, bpm_start, bpm_end)
        duration = rel[-1]
        in_first = solo_first[i + 1] - n_beats
        nxt = maps[i + 1]
        start_time = item_pos[i] + take
        start_qn = qn
        tempo_points.append(TempoPoint(start_time, bpm_start, 0))
        tempo_points.append(TempoPoint(start_time + duration, bpm_end, 1))
        out_base = len(sms[i])
        item_pos[i + 1] = start_time
        ramp_secs[i + 1] = duration
        for k in range(n_beats + 1):
            sms[i].append((take + rel[k], bm.beats[solo_end_beat + k]))
            sms[i + 1].append((rel[k], nxt.beats[in_first + k]))
            probe_points.append({"item": i + 1, "sm_index": out_base + k, "qn": start_qn + k, "time": start_time + rel[k]})
            probe_points.append({"item": i + 2, "sm_index": k, "qn": start_qn + k, "time": start_time + rel[k]})
        for frac in PHASE_FRACTIONS:
            k = min(max(round(frac * n_beats), 1), n_beats - 1)
            phase_points.append({"transition": i + 1, "beat": k, "time": start_time + rel[k]})
        stretch = 0.0
        for side, first in ((bm, solo_end_beat), (nxt, in_first)):
            for k in range(n_beats):
                src_interval = side.beats[first + k + 1] - side.beats[first + k]
                stretch = max(stretch, abs((rel[k + 1] - rel[k]) / src_interval - 1.0))
        transitions.append(TransitionPlan(
            from_order=i + 1, to_order=i + 2, bars=n_bars,
            bpm_start=bpm_start, bpm_end=bpm_end, stretch_pct=stretch * 100.0,
            start_time=start_time, end_time=start_time + duration, start_qn=start_qn,
        ))
        qn += n_beats

    items: list[ItemPlan] = []
    for i, bm in enumerate(maps):
        items.append(ItemPlan(
            order=i + 1,
            deck="A" if i % 2 == 0 else "B",
            name=names[i],
            audio=bm.audio,
            provenance=bm.provenance,
            beat_source=str(bm.source),
            in_bar=entries[i],
            bpm=solo_tempo[i],
            position=item_pos[i],
            soffs=sms[i][0][1],
            length=sms[i][-1][0],
            sms=sms[i],
            fade_in=ramp_secs[i],
            fade_out=ramp_secs[i + 1] if i < n - 1 else 0.0,
        ))
    return Plan(items, tempo_points, transitions, probe_points, phase_points, warnings)
