"""Synthetic beatmaps and a tempo-map integrator shared by the automix tests."""

from __future__ import annotations

import math
from pathlib import Path

from reaper_mcp.automix.beatmap import Beatmap
from reaper_mcp.automix.planner import TempoPoint


def make_beatmap(
    name: str = "song",
    bpm: float = 120.0,
    bars: int = 40,
    start: float = 0.5,
    short_bars: tuple[int, ...] = (),
    provenance: str = "snapped",
    wobble: float = 0.0,
) -> Beatmap:
    """`bars` bars of 4/4 at `bpm`. Bars listed in `short_bars` (1-based) have 3
    beats, which breaks the 1-2-3-4 pattern there. `wobble` adds a tiny
    deterministic timing error to every beat (fraction of a beat)."""
    beats: list[float] = []
    numbers: list[int] = []
    t = start
    interval = 60.0 / bpm
    for bar in range(1, bars + 1):
        for beat in range(1, (3 if bar in short_bars else 4) + 1):
            beats.append(t + wobble * interval * math.sin(len(beats) * 1.7))
            numbers.append(beat)
            t += interval
    # one trailing downbeat so the last bar has its closing beat
    beats.append(t)
    numbers.append(1)
    downbeats = [i for i, n in enumerate(numbers) if n == 1]
    return Beatmap(beats, numbers, downbeats, provenance, Path(f"/beats/{name}.beats"), audio=f"/music/{name}.wav")


def qn_at(points: list[TempoPoint], time: float) -> float:
    """Quarter notes elapsed at `time` under a REAPER tempo envelope
    (linear points ramp to the next point, square points hold)."""
    qn = 0.0
    for i, p in enumerate(points):
        nxt = points[i + 1] if i + 1 < len(points) else None
        seg_end = min(time, nxt.time) if nxt else time
        if seg_end <= p.time:
            break
        dt = seg_end - p.time
        if nxt and p.shape == 0:
            slope = (nxt.bpm - p.bpm) / (nxt.time - p.time)
            qn += (p.bpm * dt + slope * dt * dt / 2.0) / 60.0
        else:
            qn += p.bpm * dt / 60.0
        if not nxt or time <= nxt.time:
            break
    return qn


BIN = 0.0005
NBINS = 800


def kick(offset_bins=0):
    """Rectified envelope of one kick in the middle of a +-200 ms window."""
    env = [0.01] * NBINS
    for i in range(NBINS):
        t = (i - NBINS // 2 - offset_bins) * BIN
        if t >= 0:
            env[i] += math.exp(-t / 0.02)
    return env


def noise(seed):
    """White noise from a small LCG, so the test is deterministic."""
    state = 12345 + seed * 7919
    out = []
    for _ in range(NBINS):
        state = (state * 1103515245 + 12345) % 2**31
        out.append(state / 2**31)
    return out


class FakeReaper:
    def __init__(self, item_guids, the_plan, deck_b_offset_ms=0.0, grid_error_s=0.0, uncorrelated=False,
                 unknown_guid=False):
        self.calls = []
        self.plan = the_plan
        self.guid_to_order = {g: o for o, g in item_guids.items()}
        self.offset_bins = round(deck_b_offset_ms / (BIN * 1000))
        self.grid_error_s = grid_error_s
        self.uncorrelated = uncorrelated
        self.unknown_guid = unknown_guid

    async def execute(self, command, **params):
        self.calls.append((command, params))
        if command == "automix_grid_probe":
            results = []
            for pt in params["points"]:
                order = self.guid_to_order[pt["item_guid"]]
                item = self.plan.items[order - 1]
                take, _ = item.sms[pt["sm_index"]]
                grid_time = item.position + take
                if self.unknown_guid and pt is params["points"][0]:
                    results.append({"error": "item not found"})
                    continue
                results.append({"sm_time": grid_time + self.grid_error_s, "grid_time": grid_time})
            return {"success": True, "data": {"results": results}}
        if command == "automix_phase_envelope":
            windows = []
            for n, _ in enumerate(params["windows"]):
                if self.uncorrelated:
                    env = noise(n)
                else:
                    env = kick(self.offset_bins if n % 2 else 0)
                windows.append({"bins": env, "bin_sec": BIN})
            return {"success": True, "data": {"windows": windows}}
        raise AssertionError(command)
