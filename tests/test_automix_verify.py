"""The sync report: grid check, phase check and chunking, against a fake REAPER."""

import math

import pytest

from reaper_mcp.automix import verify as verify_mod
from reaper_mcp.automix.planner import plan
from reaper_mcp.automix.verify import best_lag, verify
from tests.automix_helpers import NBINS, FakeReaper, kick, make_beatmap

def setup(count=3, **kwargs):
    the_plan = plan([make_beatmap(f"s{i}", bpm=120.0 + i) for i in range(count)])
    guids = {i: "{G-%d}" % i for i in range(1, count + 1)}
    return the_plan, guids, FakeReaper(guids, the_plan, **kwargs)


class TestBestLag:
    def test_known_offsets(self):
        for shift in (-30, -1, 0, 1, 8, 40):
            lag, r = best_lag(kick(), kick(shift), 160)
            assert lag == shift and r > 0.99

    def test_constant_signal_has_no_lag(self):
        assert best_lag([1.0] * NBINS, [1.0] * NBINS, 160) == (0, 0.0)

    def test_short_input(self):
        assert best_lag([1.0] * 10, [1.0] * 10, 160) == (0, 0.0)


@pytest.mark.asyncio
async def test_in_sync_project_passes():
    the_plan, guids, fake = setup()
    report = await verify(fake, the_plan, guids)
    assert report["passed"] is True
    assert report["grid_max_err_us"] == 0.0
    assert report["phase_max_ms"] == 0.0
    assert len(report["points"]) == 8
    assert all(p["reliable"] and p["lag_ms"] == 0.0 for p in report["points"])
    assert report["warnings"] == [] and report["failures"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("offset_ms,passed,warned", [(0.0, True, False), (4.0, True, False),
                                                      (8.0, True, True), (20.0, False, False)])
async def test_phase_offsets_pass_warn_or_fail(offset_ms, passed, warned):
    the_plan, guids, fake = setup(deck_b_offset_ms=offset_ms)
    report = await verify(fake, the_plan, guids)
    assert report["passed"] is passed
    assert report["phase_max_ms"] == pytest.approx(offset_ms)
    assert bool([w for w in report["warnings"] if w.startswith("phase:") and "no point" not in w]) is warned
    if not passed:
        assert "20.0 ms" in report["failures"][0] and "transition 1" in report["failures"][0]


@pytest.mark.asyncio
async def test_uncorrelated_audio_is_reported_but_not_counted():
    the_plan, guids, fake = setup(uncorrelated=True)
    report = await verify(fake, the_plan, guids)
    assert report["passed"] is True
    assert report["phase_max_ms"] == 0.0
    assert report["points"] and not any(p["reliable"] for p in report["points"])
    assert any("not measured" in w for w in report["warnings"])


@pytest.mark.asyncio
async def test_grid_error_over_one_microsecond_fails():
    the_plan, guids, fake = setup(grid_error_s=5e-6)
    report = await verify(fake, the_plan, guids)
    assert report["passed"] is False
    assert report["grid_max_err_us"] == pytest.approx(5.0, rel=1e-3)
    assert report["failure_count"] == len(the_plan.probe_points)
    assert len(report["failures"]) == 20   # capped


@pytest.mark.asyncio
async def test_grid_error_under_one_microsecond_passes():
    the_plan, guids, fake = setup(grid_error_s=0.4e-6)
    report = await verify(fake, the_plan, guids)
    assert report["passed"] is True and report["grid_max_err_us"] == pytest.approx(0.4, rel=1e-3)


@pytest.mark.asyncio
async def test_missing_item_fails_the_grid_check():
    the_plan, guids, fake = setup(unknown_guid=True)
    report = await verify(fake, the_plan, guids)
    assert report["passed"] is False
    assert "item not found" in report["failures"][0]
    assert report["grid_max_err_us"] is None


@pytest.mark.asyncio
async def test_probe_points_are_sent_in_chunks(monkeypatch):
    monkeypatch.setattr(verify_mod, "MAX_AUTOMIX_PROBE_POINTS", 100)
    the_plan, guids, fake = setup()
    await verify(fake, the_plan, guids)
    probe_calls = [p for c, p in fake.calls if c == "automix_grid_probe"]
    assert len(probe_calls) == math.ceil(len(the_plan.probe_points) / 100) == 3
    assert all(len(p["points"]) <= 100 for p in probe_calls)
    assert sum(len(p["points"]) for p in probe_calls) == len(the_plan.probe_points)
    assert all(set(pt) == {"item_guid", "sm_index", "qn"} for p in probe_calls for pt in p["points"])


@pytest.mark.asyncio
async def test_one_phase_call_per_transition_with_both_decks():
    the_plan, guids, fake = setup(count=4)
    await verify(fake, the_plan, guids)
    phase_calls = [p for c, p in fake.calls if c == "automix_phase_envelope"]
    assert len(phase_calls) == 3
    for params in phase_calls:
        windows = params["windows"]
        assert len(windows) == 8
        assert [w["track_index"] for w in windows] == [0, 1] * 4
        assert all(w["dur_sec"] == 0.4 for w in windows)
        assert params["bin_sec"] == 0.0005 and params["sample_rate"] == 11025
