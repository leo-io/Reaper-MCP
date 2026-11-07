"""Lua side of automix_preview: project_open(new_tab) and the two read-only probes.

The real handlers run under lupa against a stubbed `reaper`. They live in one
contiguous block of reaper_mcp_server.lua (run_named_action ... the end of
automix_phase_envelope), so the block is cut out and executed as is.
"""

from pathlib import Path

import pytest

LUA_SRC = Path(__file__).resolve().parent.parent / "reaper_scripts" / "reaper_mcp_server.lua"


def _block() -> str:
    source = LUA_SRC.read_text(encoding="utf-8")
    start = source.index("local function run_named_action")
    end = source.index("function project.project_save(p)")
    return source[start:end]


PRELUDE = """
project = {}
calls = {}          -- ordered log of everything that mutated REAPER
destroyed = 0
created = 0
function get_numbered_track(w)
  if w.track_index == -1 then return nil, nil, "The master track is not supported by this tool" end
  if w.track_index == 99 then return nil, nil, "Track not found (index 99)" end
  return "TR" .. tostring(w.track_index), w.track_index, nil
end
function json_decode(s) error("json_decode not expected in these tests") end
"""


def make_runtime(reaper_lua: str):
    lupa = pytest.importorskip("lupa")
    rt = lupa.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(PRELUDE)
    rt.execute(reaper_lua)
    rt.execute(_block())
    return rt


def lua_list(rt, name):
    return list(rt.eval(name).values())


# ---------------------------------------------------------------- project_open

OPEN_STUB = """
action_names = {
  [41929] = "File: New project tab (ignore default template)",
  [40859] = "File: New project tab",
  [40047] = "Peaks: Build any missing peaks",
  [40295] = "View: Zoom out project",
}
dirty = 0
tracks = 0
items = 0
reaper = {
  IsProjectDirty = function() return dirty end,
  CountTracks = function() return tracks end,
  CountMediaItems = function() return items end,
  kbd_getTextFromCmd = function(cmd) return action_names[cmd] or "" end,
  Main_OnCommand = function(cmd) calls[#calls + 1] = "action:" .. cmd end,
  Main_openProject = function(path) calls[#calls + 1] = "open:" .. path end,
  GetProjectName = function() return 1, "mix.rpp" end,
  EnumProjects = function() return nil, "D:/mixes/mix.rpp" end,
  Master_GetTempo = function() return 120.0 end,
  GetProjectLength = function() return 42.0 end,
}
"""


def run_open(rt, **params):
    return rt.eval("project.project_open")(rt.table_from(params))


def test_default_open_refuses_when_project_is_dirty():
    rt = make_runtime(OPEN_STUB)
    rt.execute("dirty = 1")
    result, err = run_open(rt, path="D:/mixes/mix.rpp")
    assert result is None
    assert "unsaved changes" in err and "new_tab=true" in err
    assert lua_list(rt, "calls") == []  # Main_openProject must not run


def test_default_open_on_clean_project_opens_in_place():
    rt = make_runtime(OPEN_STUB)
    result = run_open(rt, path="D:/mixes/mix.rpp")
    assert lua_list(rt, "calls") == ["open:noprompt:D:/mixes/mix.rpp"]
    assert result["new_tab_opened"] is False
    assert result["file_path"] == "D:/mixes/mix.rpp"
    assert result["name"] == "mix.rpp"


def test_new_tab_from_a_used_project_runs_41929_then_noprompt_open():
    rt = make_runtime(OPEN_STUB)
    rt.execute("tracks = 3")
    result = run_open(rt, path="D:/mixes/mix.rpp", new_tab=True)
    assert lua_list(rt, "calls") == [
        "action:41929",
        "open:noprompt:D:/mixes/mix.rpp",
        "action:40047",
        "action:40295",
    ]
    assert result["new_tab_opened"] is True
    assert len(result["warnings"]) == 0


def test_new_tab_works_when_only_unsaved_changes_exist():
    rt = make_runtime(OPEN_STUB)
    rt.execute("dirty = 1")
    result = run_open(rt, path="D:/mixes/mix.rpp", new_tab=True)
    assert lua_list(rt, "calls")[0] == "action:41929"
    assert result["new_tab_opened"] is True


def test_new_tab_from_an_empty_clean_project_reuses_the_tab():
    rt = make_runtime(OPEN_STUB)
    result = run_open(rt, path="D:/mixes/mix.rpp", new_tab=True)
    assert "action:41929" not in lua_list(rt, "calls")
    assert "action:40859" not in lua_list(rt, "calls")
    assert result["new_tab_opened"] is False


def test_41929_name_mismatch_falls_back_to_40859():
    rt = make_runtime(OPEN_STUB)
    rt.execute('tracks = 1; action_names[41929] = "Something else entirely"')
    run_open(rt, path="D:/mixes/mix.rpp", new_tab=True)
    calls = lua_list(rt, "calls")
    assert "action:41929" not in calls
    assert calls[0] == "action:40859"
    assert "open:noprompt:D:/mixes/mix.rpp" in calls


def test_both_new_tab_actions_mismatching_is_an_error_and_runs_nothing():
    rt = make_runtime(OPEN_STUB)
    rt.execute('tracks = 1; action_names[41929] = "x"; action_names[40859] = "y"')
    result, err = run_open(rt, path="D:/mixes/mix.rpp", new_tab=True)
    assert result is None
    assert "new project tab" in err.lower()
    assert lua_list(rt, "calls") == []


def test_cosmetic_action_mismatch_is_only_a_warning():
    rt = make_runtime(OPEN_STUB)
    rt.execute('tracks = 1; action_names[40047] = "Renamed"; action_names[40295] = "Renamed"')
    result = run_open(rt, path="D:/mixes/mix.rpp", new_tab=True)
    calls = lua_list(rt, "calls")
    assert "action:40047" not in calls and "action:40295" not in calls
    assert len(result["warnings"]) == 2
    assert result["warnings"][1].startswith("action 40047")


def test_missing_path_is_an_error():
    rt = make_runtime(OPEN_STUB)
    result, err = run_open(rt)
    assert result is None and "path" in err


# ------------------------------------------------------------- automix_grid_probe

GRID_STUB = """
items = {
  {guid = "{AAAA-1}", pos = 10.0, rate = 1.0, sms = {0.0, 0.5, 1.0}},
  {guid = "{BBBB-2}", pos = 20.0, rate = 2.0, sms = {0.0, 1.0}},
}
reaper = {
  CountMediaItems = function() return #items end,
  GetMediaItem = function(_, i) return items[i + 1] end,
  GetSetMediaItemInfo_String = function(it) return true, it.guid end,
  GetActiveTake = function(it) return it end,
  GetTakeNumStretchMarkers = function(take) return #take.sms end,
  GetTakeStretchMarker = function(take, idx) return idx, take.sms[idx + 1], 0 end,
  GetMediaItemTakeInfo_Value = function(take) return take.rate end,
  GetMediaItemInfo_Value = function(it) return it.pos end,
  TimeMap2_QNToTime = function(_, qn) return qn * 0.5 end,
}
"""


def run_probe(rt, points):
    lua_points = rt.table_from([rt.table_from(p) for p in points])
    return rt.eval("project.automix_grid_probe")(rt.table_from({"points": lua_points}))


def test_grid_probe_returns_sm_and_grid_times():
    rt = make_runtime(GRID_STUB)
    result = run_probe(rt, [
        {"item_guid": "{AAAA-1}", "sm_index": 1, "qn": 8.0},
        {"item_guid": "{bbbb-2}", "sm_index": 1, "qn": 2.0},  # GUID case is ignored
    ])
    res = list(result["results"].values())
    assert res[0]["sm_time"] == pytest.approx(10.5)
    assert res[0]["grid_time"] == pytest.approx(4.0)
    assert res[1]["sm_time"] == pytest.approx(20.5)  # 20 + 1.0 / rate 2.0
    assert res[1]["grid_time"] == pytest.approx(1.0)


def test_grid_probe_reports_unknown_guid_and_bad_index_per_point():
    rt = make_runtime(GRID_STUB)
    result = run_probe(rt, [
        {"item_guid": "{NOPE}", "sm_index": 0, "qn": 0},
        {"item_guid": "{AAAA-1}", "sm_index": 3, "qn": 0},
        {"item_guid": "{AAAA-1}", "sm_index": -1, "qn": 0},
    ])
    res = list(result["results"].values())
    assert res[0]["error"] == "item not found"
    assert res[1]["error"] == "sm index out of range"
    assert res[2]["error"] == "sm index out of range"


def test_grid_probe_enforces_the_point_cap():
    rt = make_runtime(GRID_STUB)
    points = [{"item_guid": "{AAAA-1}", "sm_index": 0, "qn": 0}] * 4001
    result, err = run_probe(rt, points)
    assert result is None and "4000" in err
    ok = run_probe(rt, points[:4000])
    assert ok["count"] == 4000


def test_grid_probe_requires_points():
    rt = make_runtime(GRID_STUB)
    result, err = rt.eval("project.automix_grid_probe")(rt.table_from({}))
    assert result is None and "points" in err


# --------------------------------------------------------- automix_phase_envelope

PHASE_STUB = """
fail_read = false
reaper = {
  CreateTrackAudioAccessor = function(tr) created = created + 1; return {tr = tr} end,
  DestroyAudioAccessor = function() destroyed = destroyed + 1 end,
  new_array = function(n) return {table = function() local t = {} for i = 1, n do t[i] = (i % 2 == 0) and 0.5 or -0.5 end return t end} end,
  GetAudioAccessorSamples = function(acc, sr, ch, start, n, buf)
    if fail_read then error("boom") end
    return 1
  end,
}
"""


def run_phase(rt, windows, **extra):
    lua_windows = rt.table_from([rt.table_from(w) for w in windows])
    params = {"windows": lua_windows, **extra}
    return rt.eval("project.automix_phase_envelope")(rt.table_from(params))


def test_phase_envelope_bin_count_is_dur_over_bin_and_values_are_rectified():
    rt = make_runtime(PHASE_STUB)
    result = run_phase(rt, [{"track_index": 0, "start_sec": 1.0, "dur_sec": 0.4}])
    win = result["windows"][1]
    bins = list(win["bins"].values())
    assert len(bins) == round(0.4 / 0.0005) == 800
    assert all(b == pytest.approx(0.5, abs=0.1) for b in bins)
    assert win["bin_sec"] == pytest.approx(0.0005)
    assert rt.eval("created") == 1 and rt.eval("destroyed") == 1


def test_phase_envelope_custom_bin_size():
    rt = make_runtime(PHASE_STUB)
    result = run_phase(rt, [{"track_index": 0, "start_sec": 0.0, "dur_sec": 0.2}], bin_sec=0.002)
    assert len(list(result["windows"][1]["bins"].values())) == 100


def test_phase_envelope_destroys_accessor_when_the_read_fails():
    rt = make_runtime(PHASE_STUB)
    rt.execute("fail_read = true")
    with pytest.raises(Exception, match="boom"):
        run_phase(rt, [{"track_index": 0, "start_sec": 0.0, "dur_sec": 0.4}])
    assert rt.eval("created") == 1
    assert rt.eval("destroyed") == 1


def test_phase_envelope_caps_windows_and_duration():
    rt = make_runtime(PHASE_STUB)
    too_many = [{"track_index": 0, "start_sec": 0.0, "dur_sec": 0.1}] * 33
    result, err = run_phase(rt, too_many)
    assert result is None and "32" in err
    result, err = run_phase(rt, [{"track_index": 0, "start_sec": 0.0, "dur_sec": 1.5}])
    assert result is None and "dur_sec" in err
    ok = run_phase(rt, too_many[:32])
    assert ok["count"] == 32
    assert rt.eval("created") == rt.eval("destroyed") == 32


def test_phase_envelope_validates_before_reading_any_audio():
    rt = make_runtime(PHASE_STUB)
    windows = [
        {"track_index": 0, "start_sec": 0.0, "dur_sec": 0.1},
        {"track_index": 99, "start_sec": 0.0, "dur_sec": 0.1},
    ]
    result, err = run_phase(rt, windows)
    assert result is None and "window 2" in err
    assert rt.eval("created") == 0


def test_phase_envelope_refuses_master_and_negative_start():
    rt = make_runtime(PHASE_STUB)
    result, err = run_phase(rt, [{"track_index": -1, "start_sec": 0.0, "dur_sec": 0.1}])
    assert result is None and "master" in err.lower()
    result, err = run_phase(rt, [{"track_index": 0, "start_sec": -0.1, "dur_sec": 0.1}])
    assert result is None and "start_sec" in err
