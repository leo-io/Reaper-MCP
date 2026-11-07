"""The preview layout: order, section lengths, the tempo ramp and stretch markers."""

import pytest

from reaper_mcp.automix.planner import plan, ramp_beat_times
from reaper_mcp_shared.error_codes import ReaperMCPError
from tests.automix_helpers import make_beatmap, qn_at


def simpson_beats(b0, b1, duration, t, n=4000):
    """Beats elapsed at time `t` on a linear ramp, by Simpson integration of bpm/60."""
    h = t / n
    f = lambda x: (b0 + (b1 - b0) * x / duration) / 60.0  # noqa: E731
    total = f(0) + f(t)
    total += sum((4 if i % 2 else 2) * f(i * h) for i in range(1, n))
    return total * h / 3


class TestRamp:
    @pytest.mark.parametrize("b0,b1", [(122.0, 124.1), (128.0, 120.0), (100.0, 100.0), (90.0, 105.0)])
    def test_ramp_matches_numeric_integration(self, b0, b1):
        n_bars = 16
        times = ramp_beat_times(n_bars, b0, b1)
        duration = times[-1]
        assert len(times) == n_bars * 4 + 1
        assert duration == pytest.approx(n_bars * 4 * 60.0 / ((b0 + b1) / 2), abs=1e-12)
        for k in (1, 7, 32, 63, 64):
            assert simpson_beats(b0, b1, duration, times[k]) == pytest.approx(k, abs=1e-9)

    def test_constant_tempo_is_even_spacing(self):
        times = ramp_beat_times(4, 120.0, 120.0)
        assert times == pytest.approx([k * 0.5 for k in range(17)], abs=1e-12)


def spans_in_bars(result):
    """Source bars each song uses, from the first to the last stretch marker."""
    return [round((it.sms[-1][1] - it.sms[0][1]) / 2.0) for it in result.items]  # 120 BPM = 2 s a bar


class TestLayout:
    def test_two_three_and_five_song_layouts(self):
        for count, expected in ((2, [20, 20]), (3, [20, 36, 20]), (5, [20, 36, 36, 36, 20])):
            maps = [make_beatmap(f"s{i}", bars=60) for i in range(count)]
            assert spans_in_bars(plan(maps)) == expected

    def test_order_is_the_callers_order_and_decks_alternate(self):
        names = ["zeta", "alpha", "mid", "beta"]
        result = plan([make_beatmap(n) for n in names])
        assert [it.name for it in result.items] == names
        assert [it.order for it in result.items] == [1, 2, 3, 4]
        assert [it.deck for it in result.items] == ["A", "B", "A", "B"]

    def test_fades_cover_exactly_the_transitions(self):
        result = plan([make_beatmap(f"s{i}", bpm=120.0) for i in range(3)])
        first, mid, last = result.items
        assert first.fade_in == 0.0 and last.fade_out == 0.0
        assert first.fade_out == pytest.approx(32.0)      # 16 bars at 120 BPM
        assert mid.fade_in == pytest.approx(32.0) and mid.fade_out == pytest.approx(32.0)
        assert last.fade_in == pytest.approx(32.0)

    def test_item_positions_and_length(self):
        result = plan([make_beatmap(f"s{i}") for i in range(3)])
        a, b, c = result.items
        solo = 8.0  # 4 bars at 120 BPM
        assert a.position == 0.0 and a.sms[0] == (0.0, a.soffs)
        assert b.position == pytest.approx(solo)
        assert c.position == pytest.approx(solo + 32.0 + solo)   # song 2's solo ends, transition 2 starts
        assert a.length == pytest.approx(solo + 32.0)
        assert b.length == pytest.approx(32.0 + solo + 32.0)
        assert c.length == pytest.approx(32.0 + solo)
        for it in result.items:
            assert it.sms[0][0] == pytest.approx(0.0)
            assert it.length == it.sms[-1][0]

    def test_in_bar_is_the_first_clean_bar(self):
        result = plan([make_beatmap("a", short_bars=(3,), bars=60), make_beatmap("b", bars=60)])
        assert result.items[0].in_bar == 4
        assert result.items[1].in_bar == 1
        assert result.items[0].soffs == pytest.approx(0.5 + 2 * 2.0 + 1.5)   # two 4-beat bars and a 3-beat bar


class TestStretchMarkers:
    def test_count_is_4n_plus_1_per_side(self):
        result = plan([make_beatmap(f"s{i}") for i in range(3)])
        a, b, c = result.items
        assert len(a.sms) == 1 + 65            # solo start + outgoing transition
        assert len(b.sms) == 65 + 65           # incoming + outgoing
        assert len(c.sms) == 65 + 1            # incoming + solo end

    def test_solo_is_unstretched_exactly(self):
        maps = [make_beatmap("a", bpm=121.3, wobble=0.01), make_beatmap("b", bpm=123.9, wobble=0.01),
                make_beatmap("c", bpm=122.2, wobble=0.01)]
        result = plan(maps)
        a, b, c = result.items
        # first song: solo is the first SM pair, middle: between incoming and outgoing, last: the last pair
        for item, lo, hi in ((a, 0, 1), (b, 64, 65), (c, 64, 65)):
            d_take = item.sms[hi][0] - item.sms[lo][0]
            d_src = item.sms[hi][1] - item.sms[lo][1]
            assert d_take / d_src == pytest.approx(1.0, abs=1e-12)
            assert d_take == pytest.approx(16 * 60.0 / item.bpm, abs=1e-9)

    def test_source_times_are_the_beatmaps_beats(self):
        maps = [make_beatmap("a", wobble=0.02), make_beatmap("b", wobble=0.02)]
        result = plan(maps)
        a, b = result.items
        i0 = maps[0].downbeat_idx[a.in_bar - 1]
        assert [s for _, s in a.sms[1:]] == [maps[0].beats[i0 + 16 + k] for k in range(65)]
        j0 = maps[1].downbeat_idx[b.in_bar - 1]
        assert [s for _, s in b.sms[:65]] == [maps[1].beats[j0 + k] for k in range(65)]

    def test_both_decks_share_the_ramp_times(self):
        result = plan([make_beatmap("a", bpm=122.0), make_beatmap("b", bpm=124.0)])
        a, b = result.items
        out = a.sms[1:]
        for (out_take, _), (in_take, _) in zip(out, b.sms[:65]):
            assert out_take + a.position == pytest.approx(in_take + b.position, abs=1e-12)


class TestTempoMapAndProbes:
    def test_every_transition_starts_on_a_bar_line(self):
        result = plan([make_beatmap(f"s{i}", bpm=118 + 3 * i, wobble=0.02) for i in range(5)])
        assert len(result.transitions) == 4
        for t in result.transitions:
            assert t.start_qn % 4 == 0
        # solo 16 beats, then 64-beat transitions each followed by a 16-beat solo
        assert [t.start_qn for t in result.transitions] == [16, 96, 176, 256]

    def test_planned_times_match_an_independent_tempo_map_integration(self):
        """qn_at() re-derives REAPER's clock from the tempo points; every probe point must agree."""
        maps = [make_beatmap(f"s{i}", bpm=118 + 3.7 * i, wobble=0.02) for i in range(4)]
        result = plan(maps)
        assert result.probe_points
        worst = 0.0
        for pt in result.probe_points:
            worst = max(worst, abs(qn_at(result.tempo_points, pt["time"]) - pt["qn"]))
        assert worst < 1e-8  # quarter notes; ~1 ns at 120 BPM

    def test_probe_points_cover_every_transition_sm_on_both_decks(self):
        result = plan([make_beatmap(f"s{i}") for i in range(3)])
        assert len(result.probe_points) == 2 * 2 * 65
        for pt in result.probe_points:
            item = result.items[pt["item"] - 1]
            take, _ = item.sms[pt["sm_index"]]
            assert item.position + take == pytest.approx(pt["time"], abs=1e-12)

    def test_phase_points_are_four_per_transition_away_from_the_fade_ends(self):
        result = plan([make_beatmap(f"s{i}") for i in range(3)])
        assert len(result.phase_points) == 8
        assert [p["beat"] for p in result.phase_points[:4]] == [8, 24, 40, 56]
        assert all(0 < p["beat"] < 64 for p in result.phase_points)

    def test_tempo_points_shapes(self):
        result = plan([make_beatmap("a", bpm=122.0), make_beatmap("b", bpm=124.0)])
        shapes = [p.shape for p in result.tempo_points]
        assert shapes == [1, 0, 1]   # solo, ramp start, ramp end/solo
        assert result.tempo_points[1].bpm == pytest.approx(122.0, rel=1e-3)
        assert result.tempo_points[2].bpm == pytest.approx(124.0, rel=1e-3)
        t = result.transitions[0]
        assert (t.bpm_start, t.bpm_end) == (result.tempo_points[1].bpm, result.tempo_points[2].bpm)
        assert 1.0 < t.stretch_pct < 3.0


class TestShrinkAndErrors:
    def test_only_the_pair_that_needs_it_shrinks(self):
        # Song 2 has clean runs of at most 29 bars: it cannot hold 16+4+16.
        maps = [
            make_beatmap("a", bars=89),
            make_beatmap("b", bars=89, short_bars=(30, 60)),
            make_beatmap("c", bars=89),
            make_beatmap("d", bars=89),
        ]
        result = plan(maps)
        assert [t.bars for t in result.transitions] == [8, 16, 16]
        assert result.items[1].in_bar <= 30

    def test_shrinks_through_8_down_to_4(self):
        maps = [make_beatmap("a", bars=60), make_beatmap("b", bars=60, short_bars=(14, 28, 42))]
        # runs of 13 bars: 16+4 and 8+4 do not fit... 8+4 = 12 does fit in 13
        assert plan(maps).transitions[0].bars == 8
        maps = [make_beatmap("a", bars=60), make_beatmap("b", bars=60, short_bars=tuple(range(9, 60, 9)))]
        # runs of 8 bars: only 4+4 fits
        assert plan(maps).transitions[0].bars == 4

    def test_song_with_no_clean_span_is_an_error_naming_it(self):
        maps = [make_beatmap("good", bars=40), make_beatmap("broken", bars=40, short_bars=tuple(range(4, 41, 4)))]
        with pytest.raises(ReaperMCPError) as exc:
            plan(maps)
        assert "Song 2 (broken)" in exc.value.message
        assert "longest clean run is 3 bars" in exc.value.message

    def test_huge_tempo_jump_names_both_songs(self):
        with pytest.raises(ReaperMCPError) as exc:
            plan([make_beatmap("slow", bpm=100.0), make_beatmap("fast", bpm=125.0)])
        assert "slow" in exc.value.message and "fast" in exc.value.message and "song 1" in exc.value.message

    def test_moderate_tempo_jump_is_a_warning(self):
        result = plan([make_beatmap("a", bpm=120.0), make_beatmap("b", bpm=129.0)])
        assert any("7." in w and "song 1" in w for w in result.warnings)
        assert not plan([make_beatmap("a", bpm=120.0), make_beatmap("b", bpm=122.0)]).warnings

    def test_provenance_warnings_are_carried(self):
        result = plan([make_beatmap("a", provenance="unaligned"), make_beatmap("b", provenance="snapped")])
        assert result.warnings == []  # make_beatmap does not add loader warnings
        maps = [make_beatmap("a"), make_beatmap("b")]
        maps[0].warnings.append("beats for a may be up to 20 ms off")
        assert plan(maps).warnings == ["beats for a may be up to 20 ms off"]

    def test_one_song_is_an_error(self):
        with pytest.raises(ReaperMCPError):
            plan([make_beatmap("a")])
