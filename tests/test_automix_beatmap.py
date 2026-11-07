"""Reading beats from the sibling analysers' files, and the clean-span check."""

import hashlib
import json

import pytest

from reaper_mcp.automix.beatmap import clean_span, first_clean_bar, load_beatmap, longest_clean_run
from reaper_mcp_shared.error_codes import ErrorCode, ReaperMCPError
from tests.automix_helpers import make_beatmap


def write_beats(path, bars=8, bpm=120.0, newline="\n", blank_lines=False):
    interval = 60.0 / bpm
    lines = []
    for i in range(bars * 4 + 1):
        lines.append(f"{0.5 + i * interval:.4f}\t{(i % 4) + 1}")
        if blank_lines and i % 5 == 0:
            lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(newline.join(lines).encode() + newline.encode())


def snapped_name(audio):
    digest = hashlib.sha1(str(audio.resolve()).encode("utf-8")).hexdigest()[:10]  # noqa: S324
    return f"{audio.stem}_{digest}.beatmap.json"


def write_snapped(path, bars=8, bpm=120.0):
    interval = 60.0 / bpm
    beats = [0.5 + i * interval for i in range(bars * 4 + 1)]
    numbers = [(i % 4) + 1 for i in range(len(beats))]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"beatmap": {"beats": beats, "beat_numbers": numbers}}), encoding="utf-8")


@pytest.fixture
def audio(tmp_path):
    f = tmp_path / "song" / "track.flac"
    f.parent.mkdir()
    f.write_bytes(b"")
    return f


class TestLoadBeatmap:
    def test_snapped_cache_wins_over_beats_file(self, audio):
        write_snapped(audio.parent / ".automixer_cache" / snapped_name(audio))
        write_beats(audio.parent / "beat_this" / "track.beats")
        bm = load_beatmap(audio)
        assert bm.provenance == "snapped"
        assert bm.bar_count == 9  # 8 bars plus the closing downbeat
        assert not bm.warnings

    def test_hash_in_filename_matches_the_sibling_formula(self, audio):
        # An exact-hash file is found even when another stem_<hash> file sits beside it.
        cache = audio.parent / ".automixer_cache"
        write_snapped(cache / snapped_name(audio), bars=8)
        write_snapped(cache / "track_0123456789.beatmap.json", bars=3)
        assert load_beatmap(audio).bar_count == 9

    def test_the_only_stem_match_is_used_when_the_hash_differs(self, audio):
        write_snapped(audio.parent / ".automixer_cache" / "track_0123456789.beatmap.json")
        assert load_beatmap(audio).provenance == "snapped"

    def test_ambiguous_stem_matches_are_not_guessed(self, audio):
        cache = audio.parent / ".automixer_cache"
        write_snapped(cache / "track_0123456789.beatmap.json")
        write_snapped(cache / "track_abcdefabcd.beatmap.json")
        with pytest.raises(ReaperMCPError, match="No beats for"):
            load_beatmap(audio)

    def test_a_longer_stem_is_not_mistaken_for_this_song(self, audio):
        write_snapped(audio.parent / ".automixer_cache" / "track_extra_0123456789.beatmap.json")
        with pytest.raises(ReaperMCPError, match="No beats for"):
            load_beatmap(audio)

    def test_beats_file_without_bars_json_is_unknown_with_a_warning(self, audio):
        write_beats(audio.parent / "beat_this" / "track.beats")
        bm = load_beatmap(audio)
        assert bm.provenance == "unknown"
        assert "20 ms" in bm.warnings[0] and "track" in bm.warnings[0]

    @pytest.mark.parametrize("flag,provenance,warned", [(True, "aligned", False), (False, "unaligned", True)])
    def test_bars_json_aligned_flag(self, audio, flag, provenance, warned):
        write_beats(audio.parent / "beat_this" / "track.beats")
        (audio.parent / "beat_this" / "track.bars.json").write_text(json.dumps({"aligned": flag}))
        bm = load_beatmap(audio)
        assert bm.provenance == provenance
        assert bool(bm.warnings) is warned

    def test_beats_next_to_the_audio_are_found_after_the_beat_this_folder(self, audio):
        write_beats(audio.parent / "track.beats", bars=4)
        assert load_beatmap(audio).bar_count == 5
        write_beats(audio.parent / "beat_this" / "track.beats", bars=8)
        assert load_beatmap(audio).bar_count == 9

    def test_crlf_and_blank_lines_are_tolerated(self, audio):
        write_beats(audio.parent / "beat_this" / "track.beats", newline="\r\n", blank_lines=True)
        assert load_beatmap(audio).bar_count == 9

    def test_missing_beats_error_names_the_command(self, audio):
        with pytest.raises(ReaperMCPError) as exc:
            load_beatmap(audio)
        assert exc.value.code == ErrorCode.INVALID_PARAMETER
        assert f"automixer beatthis {audio.parent}" in exc.value.message
        assert str(audio) in exc.value.message

    def test_garbage_beats_file_is_an_error_naming_the_line(self, audio):
        p = audio.parent / "beat_this" / "track.beats"
        p.parent.mkdir()
        p.write_text("0.5\t1\nnot-a-number\t2\n")
        with pytest.raises(ReaperMCPError, match="line 2"):
            load_beatmap(audio)

    def test_non_increasing_beats_are_rejected(self, audio):
        p = audio.parent / "beat_this" / "track.beats"
        p.parent.mkdir()
        p.write_text("1.0\t1\n0.5\t2\n")
        with pytest.raises(ReaperMCPError, match="strictly increasing"):
            load_beatmap(audio)


class TestCleanSpan:
    def test_regular_music_is_clean(self):
        bm = make_beatmap(bars=30)
        assert clean_span(bm, 1, 20) == []
        assert clean_span(bm, 11, 20) == []

    def test_span_must_have_its_closing_downbeat(self):
        bm = make_beatmap(bars=20)  # 20 bars + closing downbeat
        assert clean_span(bm, 1, 20) == []
        assert clean_span(bm, 2, 20) != []

    def test_a_three_beat_bar_is_rejected(self):
        bm = make_beatmap(bars=30, short_bars=(10,))
        assert clean_span(bm, 1, 9) == []          # ends at the short bar's downbeat
        assert clean_span(bm, 1, 10) != []
        assert clean_span(bm, 10, 4) != []
        assert clean_span(bm, 11, 8) == []

    def test_a_doubled_hit_is_rejected(self):
        bm = make_beatmap(bars=8)
        # insert an extra beat 0.05 s after beat 5: intervals 0.05 / 0.45 against a 0.5 median
        bm.beats.insert(6, bm.beats[5] + 0.05)
        bm.beat_numbers.insert(6, 2)
        bm.beat_numbers[7:] = [((i + 1) % 4) + 1 for i in range(7, len(bm.beat_numbers))]
        problems = clean_span(bm, 1, 4)
        assert problems and any("interval" in p or "numbered" in p for p in problems)

    def test_interval_outliers_are_rejected_even_with_correct_numbering(self):
        bm = make_beatmap(bars=8)
        for i in range(10, len(bm.beats)):
            bm.beats[i] += 0.3  # one 0.8 s gap against a 0.5 s median
        assert any("interval" in p for p in clean_span(bm, 1, 4))

    def test_bad_arguments(self):
        bm = make_beatmap(bars=8)
        assert clean_span(bm, 0, 4) and clean_span(bm, 1, 0) and clean_span(bm, 99, 1)

    def test_first_clean_bar_and_longest_run(self):
        bm = make_beatmap(bars=40, short_bars=(6,))
        assert first_clean_bar(bm, 4) == 1
        assert first_clean_bar(bm, 10) == 7
        assert first_clean_bar(bm, 60) is None
        assert longest_clean_run(bm) == 34  # bars 7..40 plus the closing downbeat
