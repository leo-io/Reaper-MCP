"""automix_preview end to end against a fake REAPER: validation, flow and result."""

import json
from types import SimpleNamespace

import pytest

from reaper_mcp.tools.automix_tools import build_preview
from reaper_mcp_shared.error_codes import ErrorCode, ReaperMCPError
from tests.automix_helpers import FakeReaper

NAMES = ["alpha", "beta", "gamma", "delta", "epsilon"]


def make_song(folder, name, bpm=122.0, bars=48, with_beats=True, ext=".flac", short_bars=()):
    audio = folder / f"{name}{ext}"
    audio.write_bytes(b"")
    if with_beats:
        interval, lines, t = 60.0 / bpm, [], 0.5
        for bar in range(1, bars + 1):
            for beat in range(1, (3 if bar in short_bars else 4) + 1):
                lines.append(f"{t:.6f}\t{beat}")
                t += interval
        lines.append(f"{t:.6f}\t1")
        beats_dir = folder / "beat_this"
        beats_dir.mkdir(exist_ok=True)
        (beats_dir / f"{name}.beats").write_text("\n".join(lines) + "\n")
        (beats_dir / f"{name}.bars.json").write_text(json.dumps({"aligned": True}))
    return str(audio)


class ToolFake(FakeReaper):
    """FakeReaper that learns the project from the record written next to the .rpp."""

    def __init__(self, open_warnings=None, **kwargs):
        super().__init__({}, SimpleNamespace(items=[]), **kwargs)
        self.open_warnings = open_warnings or []
        self.open_error = None

    async def execute(self, command, **params):
        if command == "project_open":
            self.calls.append((command, params))
            if self.open_error:
                raise self.open_error
            record_path = params["path"].replace(".rpp", ".automix.json")
            with open(record_path, encoding="utf-8") as fh:
                rec = json.load(fh)
            self.plan = SimpleNamespace(items=[
                SimpleNamespace(position=s["position"], sms=[tuple(x) for x in s["stretch_markers"]])
                for s in rec["songs"]
            ])
            self.guid_to_order = {s["item_guid"]: s["order"] for s in rec["songs"]}
            return {"success": True, "data": {"new_tab_opened": True, "warnings": self.open_warnings}}
        return await super().execute(command, **params)


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "music"
    d.mkdir()
    return d


@pytest.fixture
def out(tmp_path):
    d = tmp_path / "mixes"
    d.mkdir()
    return d


@pytest.mark.asyncio
async def test_builds_opens_verifies_and_reports(folder, out):
    songs = [make_song(folder, n, bpm=120 + 2 * i) for i, n in enumerate(NAMES[:3])]
    fake = ToolFake()
    result = await build_preview(fake, songs, str(out), "friday_preview")

    assert result["project"] == str(out / "friday_preview.rpp")
    assert result["record"] == str(out / "friday_preview.automix.json")
    assert (out / "friday_preview.rpp").is_file() and (out / "friday_preview.automix.json").is_file()
    commands = [c for c, _ in fake.calls]
    assert commands[0] == "project_open"
    assert fake.calls[0][1] == {"path": str(out / "friday_preview.rpp"), "new_tab": True}
    assert commands[1] == "automix_grid_probe"
    assert "automix_phase_envelope" in commands

    assert result["verify"]["passed"] is True
    assert result["verify"]["grid_max_err_us"] == 0.0
    assert len(result["transitions"]) == 2
    assert all(t["bars"] == 16 for t in result["transitions"])
    assert result["tracklist"][0] == {"order": 1, "song": "alpha", "bpm": 120.0, "beats": "aligned",
                                      "in_bar": 1, "deck": "A"}
    saved = json.loads((out / "friday_preview.automix.json").read_text(encoding="utf-8"))
    assert saved["verify"]["passed"] is True            # the report is stored in the record


@pytest.mark.asyncio
async def test_order_is_exactly_the_input_order(folder, out):
    order = ["delta", "alpha", "epsilon", "beta", "gamma"]
    songs = [make_song(folder, n) for n in order]
    result = await build_preview(ToolFake(), songs, str(out), "p")
    assert [t["song"] for t in result["tracklist"]] == order
    assert [t["order"] for t in result["tracklist"]] == [1, 2, 3, 4, 5]
    saved = json.loads((out / "p.automix.json").read_text(encoding="utf-8"))
    assert [s["name"] for s in saved["songs"]] == order
    rpp = (out / "p.rpp").read_text(encoding="utf-8")
    item_names = [ln.split('"')[1] for ln in rpp.splitlines() if ln.strip().startswith("NAME ") and ". " in ln]
    assert sorted(item_names) == sorted(f"{i + 1:02d}. {n}" for i, n in enumerate(order))


@pytest.mark.asyncio
async def test_verify_failure_is_returned_not_raised(folder, out):
    songs = [make_song(folder, n) for n in NAMES[:2]]
    result = await build_preview(ToolFake(deck_b_offset_ms=25.0), songs, str(out), "p")
    assert result["verify"]["passed"] is False
    assert "25.0 ms" in result["verify"]["failures"][0]
    assert json.loads((out / "p.automix.json").read_text(encoding="utf-8"))["verify"]["passed"] is False


@pytest.mark.asyncio
async def test_old_bridge_without_the_probes_is_reported_as_a_failed_check(folder, out):
    class OldBridge(ToolFake):
        async def execute(self, command, **params):
            if command.startswith("automix_"):
                raise ReaperMCPError(ErrorCode.COMMAND_FAILED, "Unknown command: " + command)
            return await super().execute(command, **params)

    songs = [make_song(folder, n) for n in NAMES[:2]]
    result = await build_preview(OldBridge(), songs, str(out), "p")
    assert result["verify"]["passed"] is False
    assert "verify could not run" in result["verify"]["failures"][0]
    assert "Unknown command" in result["verify"]["failures"][0]


@pytest.mark.asyncio
async def test_every_song_without_beats_is_listed_in_one_error(folder, out):
    songs = [make_song(folder, "ok"), make_song(folder, "nobeats1", with_beats=False),
             make_song(folder, "fine"), make_song(folder, "nobeats2", with_beats=False)]
    fake = ToolFake()
    with pytest.raises(ReaperMCPError) as exc:
        await build_preview(fake, songs, str(out), "p")
    msg = exc.value.message
    assert "2 of 4 songs" in msg and "songs[2]" in msg and "songs[4]" in msg
    assert msg.count("automixer beatthis") == 2 and "nobeats1" in msg and "nobeats2" in msg
    assert fake.calls == [] and list(out.iterdir()) == []


@pytest.mark.asyncio
async def test_existing_output_is_an_error_and_untouched(folder, out):
    songs = [make_song(folder, n) for n in NAMES[:2]]
    (out / "p.rpp").write_text("keep me")
    with pytest.raises(ReaperMCPError, match="already exists"):
        await build_preview(ToolFake(), songs, str(out), "p")
    assert (out / "p.rpp").read_text() == "keep me"
    assert not (out / "p.automix.json").exists()
    (out / "q.automix.json").write_text("{}")
    with pytest.raises(ReaperMCPError, match="q.automix.json"):
        await build_preview(ToolFake(), songs, str(out), "q")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["rel/path.flac", "song.flac"])
async def test_relative_path_is_an_error_naming_it(folder, out, bad):
    songs = [make_song(folder, "a"), bad]
    with pytest.raises(ReaperMCPError) as exc:
        await build_preview(ToolFake(), songs, str(out), "p")
    assert repr(bad) in exc.value.message and "absolute" in exc.value.message


@pytest.mark.asyncio
async def test_bad_songs_are_all_named(folder, out):
    good = make_song(folder, "a")
    (folder / "doc.txt").write_text("x")
    songs = [good, str(folder / "missing.flac"), str(folder / "doc.txt"), 42]
    with pytest.raises(ReaperMCPError) as exc:
        await build_preview(ToolFake(), songs, str(out), "p")
    msg = exc.value.message
    assert "missing.flac" in msg and "does not exist" in msg
    assert "doc.txt" in msg and "unsupported audio type" in msg
    assert "42" in msg


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 101])
async def test_song_count_limits(folder, out, count):
    song = make_song(folder, "a")
    with pytest.raises(ReaperMCPError, match="2-100"):
        await build_preview(ToolFake(), [song] * count, str(out), "p")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "a/b", "..", ".hidden", "trailing.", " pad", "x" * 81, "bad\nname", 'q"q', None])
async def test_bad_project_names(folder, out, name):
    songs = [make_song(folder, n) for n in NAMES[:2]]
    with pytest.raises(ReaperMCPError, match="project_name"):
        await build_preview(ToolFake(), songs, str(out), name)


@pytest.mark.asyncio
async def test_output_dir_must_be_absolute_and_exist(folder, out, tmp_path):
    songs = [make_song(folder, n) for n in NAMES[:2]]
    with pytest.raises(ReaperMCPError, match="absolute"):
        await build_preview(ToolFake(), songs, "mixes", "p")
    with pytest.raises(ReaperMCPError, match="does not exist"):
        await build_preview(ToolFake(), songs, str(tmp_path / "nope"), "p")


@pytest.mark.asyncio
async def test_unplaceable_song_is_an_error_naming_it(folder, out):
    songs = [make_song(folder, "ok"), make_song(folder, "choppy", short_bars=tuple(range(3, 49, 3)))]
    with pytest.raises(ReaperMCPError) as exc:
        await build_preview(ToolFake(), songs, str(out), "p")
    assert "Song 2 (choppy)" in exc.value.message
    assert list(out.iterdir()) == []                      # nothing written


@pytest.mark.asyncio
async def test_open_warnings_and_beat_warnings_are_collected(folder, out):
    songs = [make_song(folder, "a"), make_song(folder, "b")]
    (folder / "beat_this" / "b.bars.json").unlink()      # provenance becomes unknown
    result = await build_preview(ToolFake(open_warnings=["action 40295 skipped"]), songs, str(out), "p")
    assert any("20 ms" in w and "b" in w for w in result["warnings"])
    assert "action 40295 skipped" in result["warnings"]
    assert result["tracklist"][1]["beats"] == "unknown"


@pytest.mark.asyncio
async def test_a_failed_open_is_raised(folder, out):
    fake = ToolFake()
    fake.open_error = ReaperMCPError(ErrorCode.COMMAND_FAILED, "Current project has unsaved changes")
    songs = [make_song(folder, n) for n in NAMES[:2]]
    with pytest.raises(ReaperMCPError, match="unsaved changes"):
        await build_preview(fake, songs, str(out), "p")
