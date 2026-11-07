"""Prove the preview is in sync, by asking REAPER.

Two independent checks, run on every build:

* **Grid check** (``automix_grid_probe``): every transition stretch marker must
  sit where the tempo map says its beat is, and where the plan says. Tolerance 1 microsecond.
* **Phase check** (``automix_phase_envelope``): around four beats of each
  transition, the audio of the two decks is compared by cross-correlating
  their rectified envelopes. A lag over 5 ms warns, over 15 ms fails. A window
  with no shared beat-locked content (correlation under 0.5) is reported as
  unreliable and does not count either way.

The cross-correlation is a pure-Python port of ``conga_reaper_automix/rb2reaper/verify.py``.
"""

from __future__ import annotations

import math

from reaper_mcp_shared.constants import MAX_AUTOMIX_PHASE_WINDOWS, MAX_AUTOMIX_PROBE_POINTS
from reaper_mcp_shared.error_codes import ErrorCode, ReaperMCPError

from .planner import Plan

GRID_TOLERANCE_S = 1e-6
SAMPLE_RATE = 11025
WINDOW_S = 0.4              # +-200 ms around the beat
BIN_S = 0.0005
MAX_LAG_S = 0.080
WARN_MS = 5.0
FAIL_MS = 15.0
MIN_CORRELATION = 0.5
MAX_REPORTED_FAILURES = 20
DECK_TRACK = {"A": 0, "B": 1}


def best_lag(env_a: list[float], env_b: list[float], max_lag: int) -> tuple[int, float]:
    """Lag in bins of `env_b` relative to `env_a` with the highest Pearson correlation.

    Positive lag means B is late. Compares the centre of `env_a` against `env_b`
    shifted by -max_lag..+max_lag; returns (0, 0.0) when nothing can be correlated.
    """
    n = len(env_a)
    lo, hi = max_lag, n - max_lag
    m = hi - lo
    if m < 4 or len(env_b) < n:
        return 0, 0.0
    ref = env_a[lo:hi]
    mean_ref = sum(ref) / m
    centred = [x - mean_ref for x in ref]
    norm_ref = math.sqrt(sum(x * x for x in centred))
    if norm_ref == 0.0:
        return 0, 0.0
    # running sum and sum of squares of env_b[lo + lag : hi + lag]
    seg_sum = sum(env_b[lo - max_lag:hi - max_lag])
    seg_sq = sum(x * x for x in env_b[lo - max_lag:hi - max_lag])
    best_lag_bins, best_r = 0, -2.0
    for lag in range(-max_lag, max_lag + 1):
        start = lo + lag
        if lag > -max_lag:
            seg_sum += env_b[hi + lag - 1] - env_b[start - 1]
            seg_sq += env_b[hi + lag - 1] ** 2 - env_b[start - 1] ** 2
        variance = seg_sq - seg_sum * seg_sum / m
        if variance <= 1e-18:
            continue
        dot = sum(c * env_b[start + i] for i, c in enumerate(centred))
        r = dot / (norm_ref * math.sqrt(variance))
        if r > best_r:
            best_lag_bins, best_r = lag, r
    return (best_lag_bins, best_r) if best_r > -2.0 else (0, 0.0)


def _grid_response(response: dict) -> list[dict]:
    data = response.get("data", response)
    return data.get("results", [])


async def _check_grid(client, plan: Plan, item_guids: dict[int, str], failures: list[str]) -> float:
    points = plan.probe_points
    worst = 0.0
    for start in range(0, len(points), MAX_AUTOMIX_PROBE_POINTS):
        chunk = points[start:start + MAX_AUTOMIX_PROBE_POINTS]
        payload = [{"item_guid": item_guids[p["item"]], "sm_index": p["sm_index"], "qn": p["qn"]} for p in chunk]
        results = _grid_response(await client.execute("automix_grid_probe", points=payload))
        if len(results) != len(chunk):
            raise ReaperMCPError(
                ErrorCode.COMMAND_FAILED,
                f"automix_grid_probe returned {len(results)} results for {len(chunk)} points",
            )
        for planned, got in zip(chunk, results):
            label = f"song {planned['item']} stretch marker {planned['sm_index']}"
            if "error" in got:
                failures.append(f"grid: {label}: {got['error']}")
                worst = math.inf
                continue
            sm_time, grid_time = got["sm_time"], got["grid_time"]
            err = max(abs(sm_time - grid_time), abs(sm_time - planned["time"]), abs(grid_time - planned["time"]))
            worst = max(worst, err)
            if err > GRID_TOLERANCE_S:
                failures.append(
                    f"grid: {label}: stretch marker at {sm_time:.9f}s, tempo map beat at "
                    f"{grid_time:.9f}s, planned {planned['time']:.9f}s"
                )
    return worst * 1e6


async def _check_phase(client, plan: Plan, failures: list[str], warnings: list[str]) -> tuple[float, list[dict]]:
    by_transition: dict[int, list[dict]] = {}
    for p in plan.phase_points:
        by_transition.setdefault(p["transition"], []).append(p)
    report: list[dict] = []
    worst = 0.0
    for transition, pts in sorted(by_transition.items()):
        usable = [p for p in pts if p["time"] - WINDOW_S / 2 >= 0.0]
        windows = [
            {"track_index": DECK_TRACK[deck], "start_sec": p["time"] - WINDOW_S / 2, "dur_sec": WINDOW_S}
            for p in usable for deck in ("A", "B")
        ]
        if not windows:
            continue
        if len(windows) > MAX_AUTOMIX_PHASE_WINDOWS:
            raise ReaperMCPError(ErrorCode.COMMAND_FAILED, "Too many phase windows for one transition")
        response = await client.execute(
            "automix_phase_envelope", windows=windows, sample_rate=SAMPLE_RATE, bin_sec=BIN_S,
        )
        envelopes = response.get("data", response).get("windows", [])
        if len(envelopes) != len(windows):
            raise ReaperMCPError(
                ErrorCode.COMMAND_FAILED,
                f"automix_phase_envelope returned {len(envelopes)} windows for {len(windows)} requested",
            )
        for n, p in enumerate(usable):
            env_a, env_b = envelopes[2 * n], envelopes[2 * n + 1]
            bin_sec = env_a["bin_sec"]
            lag, r = best_lag(env_a["bins"], env_b["bins"], int(MAX_LAG_S / bin_sec))
            lag_ms = lag * bin_sec * 1000.0
            reliable = r >= MIN_CORRELATION
            label = f"transition {transition} beat {p['beat']}"
            report.append({
                "transition": transition, "beat": p["beat"], "lag_ms": round(lag_ms, 3),
                "r": round(r, 3), "reliable": reliable,
            })
            if not reliable:
                continue
            worst = max(worst, abs(lag_ms))
            if abs(lag_ms) > FAIL_MS:
                failures.append(f"phase: {label}: decks are {lag_ms:+.1f} ms apart (limit {FAIL_MS:g} ms, r={r:.2f})")
            elif abs(lag_ms) > WARN_MS:
                warnings.append(f"phase: {label}: decks drift {lag_ms:+.1f} ms apart (warning above {WARN_MS:g} ms, r={r:.2f})")
    return worst, report


async def verify(client, plan: Plan, item_guids: dict[int, str]) -> dict:
    """Run both checks against the project open in REAPER and return the report."""
    failures: list[str] = []
    warnings: list[str] = []
    grid_err_us = await _check_grid(client, plan, item_guids, failures)
    phase_ms, points = await _check_phase(client, plan, failures, warnings)
    if not any(p["reliable"] for p in points):
        warnings.append("phase: no point had a reliable correlation, so the audio phase was not measured")
    return {
        "passed": not failures,
        "grid_max_err_us": None if math.isinf(grid_err_us) else round(grid_err_us, 4),
        "phase_max_ms": round(phase_ms, 3),
        "points": points,
        "failures": failures[:MAX_REPORTED_FAILURES],
        "failure_count": len(failures),
        "warnings": warnings,
    }
