"""RPP quoting, the project builder, the golden file and the record."""

import itertools
import json
import os
from pathlib import Path

import pytest

from reaper_mcp.automix import record
from reaper_mcp.automix.planner import plan
from reaper_mcp.automix.rpp_builder import build_rpp
from reaper_mcp.automix.rpp_quote import clean_label, rpp_quote
from reaper_mcp_shared.error_codes import ErrorCode, ReaperMCPError
from tests.automix_helpers import make_beatmap

GOLDEN = Path(__file__).parent / "fixtures" / "automix" / "preview_3songs.rpp"


def counter_guids():
    counter = itertools.count(1)
    return lambda: "{%08X-0000-0000-0000-000000000000}" % next(counter)


def three_song_plan():
    maps = []
    for name, bpm in (("alpha", 122.0), ("beta", 124.0), ("gamma", 123.0)):
        bm = make_beatmap(name, bpm=bpm, bars=44)
        bm.audio = f"D:\\music\\{name}.flac" if name == "alpha" else f"D:\\music\\{name}.mp3"
        maps.append(bm)
    return plan(maps)


class TestQuote:
    def test_plain_text_uses_double_quotes(self):
        assert rpp_quote("abc", is_path=False) == '"abc"'

    def test_text_with_double_quotes_uses_single(self):
        assert rpp_quote('say "hi"', is_path=False) == "'say \"hi\"'"

    def test_text_with_both_uses_backtick(self):
        assert rpp_quote("it's \"x\"", is_path=True) == "`it's \"x\"`"

    def test_text_with_all_three_has_double_quotes_replaced(self):
        assert rpp_quote("a\"b'c`d", is_path=False) == "\"a'b'c`d\""

    def test_path_with_all_three_is_rejected(self):
        with pytest.raises(ReaperMCPError) as exc:
            rpp_quote("a\"b'c`d", is_path=True)
        assert exc.value.code == ErrorCode.INVALID_PATH

    @pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\0b", "x\n  <ITEM"])
    @pytest.mark.parametrize("is_path", [True, False])
    def test_line_breaks_and_nul_are_rejected(self, bad, is_path):
        with pytest.raises(ReaperMCPError):
            rpp_quote(bad, is_path=is_path)

    def test_clean_label(self):
        assert clean_label("  Song\t\x00 name\x1b\n  two ") == "Song name two"
        assert len(clean_label("x" * 500)) == 120


class TestBuilder:
    def test_golden_three_song_project(self):
        text = build_rpp(three_song_plan(), counter_guids()).text
        if os.environ.get("UPDATE_GOLDEN"):
            GOLDEN.write_text(text, encoding="utf-8", newline="\n")
        assert text == GOLDEN.read_text(encoding="utf-8")

    def test_item_guids_are_unique_and_returned_by_order(self):
        result = build_rpp(three_song_plan())
        assert sorted(result.item_guids) == [1, 2, 3]
        iguids = [ln.split()[1] for ln in result.text.splitlines() if ln.strip().startswith("IGUID")]
        take_guids = [ln.split()[1] for ln in result.text.splitlines() if ln.strip().startswith("GUID")]
        assert len(set(iguids + take_guids)) == 6
        assert set(iguids) == set(result.item_guids.values())

    def test_duplicate_guids_are_refused(self):
        with pytest.raises(ReaperMCPError, match="Duplicate GUID"):
            build_rpp(three_song_plan(), lambda: "{SAME}")

    def test_source_tag_per_extension(self):
        for ext, tag in ((".wav", "WAVE"), (".aiff", "WAVE"), (".flac", "FLAC"), (".mp3", "MP3"),
                         (".ogg", "VORBIS"), (".FLAC", "FLAC")):
            maps = [make_beatmap("a"), make_beatmap("b")]
            maps[0].audio = f"/music/a{ext}"
            assert f"<SOURCE {tag}" in build_rpp(plan(maps)).text

    def test_decks_hold_alternating_songs_in_order(self):
        text = build_rpp(three_song_plan()).text
        deck_a, deck_b = text.split('NAME "Deck B"')
        assert '"01. alpha"' in deck_a and '"03. gamma"' in deck_a and '"02. beta"' not in deck_a
        assert '"02. beta"' in deck_b

    def test_sm_lines_have_24_pairs_and_use_plus_separators(self):
        lines = [ln for ln in build_rpp(three_song_plan()).text.splitlines() if ln.strip().startswith("SM ")]
        assert lines
        assert all(ln.count(" + ") <= 23 for ln in lines)
        assert max(ln.count(" + ") for ln in lines) == 23
        total_pairs = sum(ln.count(" + ") + 1 for ln in lines)
        assert total_pairs == 66 + 130 + 66

    def test_header_and_tempo_envelope(self):
        text = build_rpp(three_song_plan()).text
        for line in ("TIMELOCKMODE 0", "TEMPOENVLOCKMODE 1", "DEFPITCHMODE 589824 0", "SAMPLERATE 44100 0 0"):
            assert f"\n  {line}\n" in text
        pts = [ln for ln in text.splitlines() if ln.strip().startswith("PT ")]
        assert len(pts) == 1 + 2 * 2
        assert "262148" in pts[0] and all("262148" not in p for p in pts[1:])
        assert text.count("BEAT 0") == 2 + 3                   # two tracks, three items
        assert text.count("PLAYRATE 1 1 0 -1 5 0.0025") == 3

    def test_hostile_names_cannot_inject_project_lines(self):
        maps = [make_beatmap("a"), make_beatmap("b")]
        maps[0].audio = '/music/ev"il\'s`.wav'
        with pytest.raises(ReaperMCPError):
            build_rpp(plan(maps))
        maps[0].audio = '/music/he said "x" \'y\'.wav'
        text = build_rpp(plan(maps)).text
        assert "FILE `/music/he said \"x\" 'y'.wav`" in text


class TestRecord:
    def test_record_round_trip(self, tmp_path):
        p = three_song_plan()
        built = build_rpp(p)
        path = tmp_path / "mix.automix.json"
        record.write_record(path, record.record_dict(p, built.item_guids, str(tmp_path / "mix.rpp")))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["schema"] == 1 and data["verify"] is None
        assert [s["order"] for s in data["songs"]] == [1, 2, 3]
        assert data["songs"][1]["item_guid"] == built.item_guids[2]
        assert len(data["songs"][1]["stretch_markers"]) == 130
        assert len(data["transitions"]) == 2
        record.save_verify(path, {"passed": True})
        assert json.loads(path.read_text(encoding="utf-8"))["verify"] == {"passed": True}

    def test_existing_record_is_never_overwritten(self, tmp_path):
        path = tmp_path / "mix.automix.json"
        path.write_text("{}")
        with pytest.raises(FileExistsError):
            record.write_record(path, {})
        assert path.read_text() == "{}"
