"""The live probe generator produces a valid project and a Lua script that parses.

Running the probe needs REAPER (see scripts/live_probes/gen_automix_probe.py);
this guards the generator itself, and ties its ramp formula to the planner's.
"""

import importlib.util
import math
from pathlib import Path

import pytest

from reaper_mcp.automix.planner import ramp_beat_times

PROBE_DIR = Path(__file__).resolve().parent.parent / "scripts" / "live_probes"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_automix_probe", PROBE_DIR / "gen_automix_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_ramp_is_the_planners_ramp(gen):
    duration, times = gen.ramp_times()
    planned = ramp_beat_times(gen.RAMP_BEATS // 4, gen.B0, gen.B1)
    assert duration == pytest.approx(planned[-1], abs=1e-12)
    assert times == pytest.approx(planned, abs=1e-12)


def test_generated_files_are_complete_and_the_lua_parses(gen, tmp_path):
    lupa = pytest.importorskip("lupa")
    rpp_path, lua_path = gen.build(tmp_path / "out", tmp_path / "scripts")
    rpp = rpp_path.read_text(encoding="utf-8")
    assert rpp.count("<TRACK") == 4 and rpp.count("<ITEM") == 4 and rpp.count("PT ") == 2
    lua = lua_path.read_text(encoding="utf-8")
    assert "@@" not in lua
    lupa.LuaRuntime().compile(lua)               # raises on a syntax error
    assert (tmp_path / "out" / "kicks120.wav").stat().st_size > 1000
    assert math.isclose(gen.B0, 120.0)
