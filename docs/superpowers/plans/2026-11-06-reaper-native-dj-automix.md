# REAPER-Native DJ Automix v1 — Preview Minimix (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan slice-by-slice. Steps use checkbox (`- [ ]`) syntax for tracking. Each slice is one PR; every slice leaves `ruff check .` and `pytest tests/ -x -q` green.

**Goal (the only v1 feature):** Pass an ordered list of songs to the MCP and get a *preview minimix* open in REAPER: the songs **in exactly that order**, each pair joined by a beat-synced transition, with a report that proves the sync.

**Out of v1** (all in [`2026-11-06-dj-automix-roadmap.md`](2026-11-06-dj-automix-roadmap.md)): playlists and directory scans, tag/key reading, automatic ordering, full-length mix (Stage 2), edit sync, CSV/session editing, configurable transition/solo/fade/gain, half/double-time, rendering, cue sheets, ruler lanes, EQ/loudness, and beat analysis.

## What v1 produces

One MCP tool call:

```
automix_preview(songs=["D:/music/a/a.flac", "D:/music/b/b.mp3", "D:/music/c/c.wav"],
                output_dir="D:/mixes", project_name="friday_preview")
```

writes `D:/mixes/friday_preview.rpp` and `D:/mixes/friday_preview.automix.json`, opens the `.rpp` in a **new REAPER tab**, checks it against REAPER, and returns:

```
{ "project": ".../friday_preview.rpp",
  "tracklist": [{"order": 1, "song": "a", "bpm": 122.0, "beats": "snapped", "in_bar": 9, "deck": "A"}, ...],
  "transitions": [{"from": 1, "to": 2, "bars": 16, "bpm_start": 122.0, "bpm_end": 124.1, "stretch_pct": 1.7}, ...],
  "warnings": [...],
  "verify": {"passed": true, "grid_max_err_us": 0.0, "phase_max_ms": 0.8, "points": [...]} }
```

Fixed layout (constants in v1, configurable in v2):

```
song 1:  [ solo 4 ][ transition 16 ]
song 2:            [ transition 16 ][ solo 4 ][ transition 16 ]
song 3:                                       [ transition 16 ][ solo 4 ]
          Deck A / Deck B alternate. Transitions shrink 16 → 8 → 4 bars only when a song
          has no clean span long enough.
```

- **Solo bars:** played at 1.0×, untouched.
- **Transition bars:** both decks use per-beat stretch markers onto one linear tempo ramp (outgoing song's tempo → incoming song's tempo), with near-equal-power item fades (shape 1).

## Facts this plan relies on (measured in REAPER 7.79/x64, 2026-11-06)

Measured with a probe in its own tab (synthetic kicks, track audio accessors). Treat these as constants; Slice 3's live probe re-checks them.

| Fact | Measured |
|---|---|
| Linear tempo ramp (`PT … 0`) is linear in **time**; beat k at `t_k = (−b0 + √(b0² + 120·s·k))/s`, `T = 4N·60/((b0+b1)/2)`, `s = (b1−b0)/T` | matches `TimeMap2_QNToTime` to 4.5e-13 s |
| Per-beat `SM` + `PLAYRATE 1 1 0 -1 5 0.0025` (mode 5, 2.5 ms fade) + `DEFPITCHMODE 589824 0` (élastique 3.3.3 Pro) | deck vs grid ≤ 0.43 ms at up to 8% stretch; **deck A vs deck B ≤ 0.45 ms** |
| `SM <take_pos> <src_pos>`: the second value is the **absolute** source time | ✔ |
| `BEAT 0` on item and track → time timebase | ✔ |
| Fade shape 1 = `x(2−x)`, mirrored for fade-out; crossfade power +0.51 dB max (near equal-power). Shape 6 is a steep S-curve | ✔ |
| RPP strings: REAPER reads and writes `"…"`, `'…'`, `` `…` `` | ✔ |
| `IGUID` → item `GUID`; REAPER **keeps duplicates**, so uniqueness is the builder's job | ✔ |
| 41929 = "New project tab, ignore default template"; 40859 = "New project tab"; 40047 = "Peaks: Build any missing peaks"; 40295 = "View: Zoom out project" | ✔ |
| `Main_openProject("noprompt:" .. path)` after 41929 opens in the new tab, not dirty | ✔ |
| `CreateTrackAudioAccessor` output includes stretch markers and item fades | ✔ |

## Global Constraints

- **Order is the caller's order.** No reordering, no dropping. A song that can't be placed is an error naming that song; nothing is skipped silently.
- **No beat analysis here.** Beats come from the sibling analysers (`conga_automixer` `automixer beatthis`, `automixer_mcp` snapped beatmaps). Missing beats → error with the command that creates them.
- **Bars are source bars** (from the beatmap). The REAPER grid only has to be exact inside transitions; in solos it's display-only.
- **Verify every build.** The result always contains the grid check and phase check from Slice 3. A failing check is reported (`verify.passed=false`) and never hidden.
- **Lua:** every new or changed handler gets a lupa test and an `UNDO_EXEMPT_COMMANDS` entry. All three v1 handlers trip `test_lua_undo_points.py`'s `MUTATING_CALL` regex (`Main_OnCommand`/`Main_openProject`, `GetSet…Info_String`, `Create…`). Main actions are name-checked with `kbd_getTextFromCmd` before running.
- **Validation:** `songs` has 2–`MAX_AUTOMIX_SONGS` (new constant, 100) absolute existing audio paths (`.wav .flac .mp3 .aif .aiff .ogg .m4a`), each through `safe_path`. `output_dir` absolute and existing; `project_name` matches `^[A-Za-z0-9 _.-]{1,80}$`. An existing output file → error (choose another name; overwrite is v2). Errors name the bad value.
- `CHANGELOG.md` `[Unreleased]` entry per slice; `docs/TOOLS.md` and `README.md` tool counts in Slice 3.

## Prerequisite (sibling repo, recommended)

`conga_automixer`: write `"aligned": true|false` into `<stem>.bars.json` from `beatthis.py` (alignment happens by default but isn't recorded). Without it, conga `.beats` load with provenance `unknown` and a warning, and the phase check is the guard.

---

## Slice 1: Lua bridge

**Files:** `reaper_scripts/reaper_mcp_server.lua`, `reaper_mcp/tools/project_tools.py`, `tests/test_lua_project_open.py`, `tests/test_lua_automix_probe.py`, `tests/test_project_tools.py`

### Task 1.1: `project_open(new_tab=…)`
- `new_tab=False` (default): if `IsProjectDirty(0) ~= 0`, return error `"Current project has unsaved changes; save it first or pass new_tab=true"` and **don't** call `Main_openProject`. Today's call can open a modal save dialog and freeze the bridge.
- `new_tab=True`: if the current tab has tracks, items or unsaved changes, run 41929 (text must contain `"New project tab"`; fall back to 40859; if neither matches, error). Then `Main_openProject("noprompt:" .. path)`. Then run 40047 and 40295 (each name-checked; on mismatch, skip and add a warning, not an error).
- Returns the current fields plus `file_path` (`EnumProjects(-1)`), `new_tab_opened`, `warnings`.
- Python: absolute `.rpp` path via `safe_path`; new `new_tab: bool = False` arg; docstring updated.
- [ ] lupa: dirty + no new tab → error, no open; clean → open; new tab → 41929 then `noprompt:`; 41929 text mismatch → 40859; both mismatch → error, nothing run.
- [ ] `UNDO_EXEMPT_COMMANDS["project_open"]` reason: "replaces the project or opens a new tab".

### Task 1.2: `automix_grid_probe` (read-only)
Input `points=[{item_guid, sm_index, qn}]` (≤ 4000). Build a `GUID → item` map once (`GetSetMediaItemInfo_String(it, "GUID", "", false)`). For each point return `{sm_time = D_POSITION + sm_pos / D_PLAYRATE, grid_time = TimeMap2_QNToTime(0, qn)}`, or `{error = "item not found" | "sm index out of range"}`.
- [ ] lupa: found and not-found GUIDs; out-of-range index; cap enforced.
- [ ] `UNDO_EXEMPT_COMMANDS`: "read-only timing probe".

### Task 1.3: `automix_phase_envelope` (read-only)
Input `windows=[{track_index, start_sec, dur_sec}]` (≤ 32 windows, `dur_sec ≤ 1.0`), `sample_rate=11025`, `bin_sec=0.0005`. For each window: `CreateTrackAudioAccessor` → `GetAudioAccessorSamples` (mono) → rectified bin means → `DestroyAudioAccessor`, **also on error** (wrap the per-window read in `pcall`, destroy, then re-raise). Returns `bins` per window.
- [ ] lupa: bin count = `round(dur/bin)`; accessor destroyed on success and when `GetAudioAccessorSamples` errors; caps.
- [ ] `UNDO_EXEMPT_COMMANDS`: "read-only audio analysis via audio accessor".

---

## Slice 2: Automix core (pure Python, stdlib only)

**Files:** `reaper_mcp/automix/__init__.py`, `beatmap.py`, `planner.py`, `rpp_quote.py`, `rpp_builder.py`, `record.py`; tests `tests/test_automix_beatmap.py`, `test_automix_planner.py`, `test_automix_rpp.py`; golden file `tests/fixtures/automix/preview_3songs.rpp`

### Task 2.1: `beatmap.py`
`load_beatmap(audio_path) -> Beatmap(beats: list[float], beat_numbers: list[int], downbeat_idx: list[int], provenance: str, source: Path)`. Search order, first hit wins:
1. `<dir>/.automixer_cache/<stem>_<sha1(str(Path(audio).resolve()))[:10]>.beatmap.json` (or the only `<stem>_*.beatmap.json` there); read `["beatmap"]["beats" | "beat_numbers"]` → `provenance="snapped"`
2. `<dir>/beat_this/<stem>.beats`, then `<dir>/<stem>.beats` (`time\tbeat_number` per line), with `<stem>.bars.json` next to it: `"aligned": true` → `"aligned"`, `false` → `"unaligned"`, no file/flag → `"unknown"`
3. none → `ReaperMCPError(INVALID_PARAMETER, "No beats for <path>: run `automixer beatthis <dir>`")`

`unaligned` and `unknown` add a warning (`"beats for <song> may be up to 20 ms off; verify will measure it"`).
`clean_span(bm, first_bar, n_bars) -> list[str]`: beat numbers 1-2-3-4 with no gaps, each interval within 0.67–1.5× the span median, and the downbeat after the span exists.
- [ ] Tests: each provenance tier; hash filename matches the sibling formula; a 3-beat bar and a doubled hit are rejected; CRLF and blank lines in `.beats` tolerated.

### Task 2.2: `planner.py`
Input: ordered `Beatmap`s. Constants: `TRANSITION_BARS = 16` (shrinks 8, 4), `SOLO_BARS = 4`, `FADE_SHAPE = 1`.

1. **Spans:** song i needs `T(i−1,i) + 4 + T(i,i+1)` bars (first/last song: one transition). `in_bar` = first bar ≥ 1 where `clean_span` passes for the whole span.
2. **Shrink per pair:** start every `T = 16`. While some song has no clean span, halve the larger of its adjacent `T`s (16 → 8 → 4). If it's still impossible at 4 → error naming the song and its longest clean run.
3. **Tempos:** `bpm_start` of pair (i,i+1) = song i's mean tempo over its outgoing transition beats; `bpm_end` = song i+1's mean over its incoming beats. `|Δ|/bpm_start > 16%` → error naming the pair (half/double and forcing are v2); `> 6%` → warning.
4. **Timeline:** `P` = project time cursor, `Q` = quarter-note cursor (`Q` advances by 4 per bar, so every transition starts on a grid bar line).
   - Solo: unstretched; duration = source time from its first downbeat to the downbeat after; tempo point square at `16·60/duration` (makes the grid bar-exact at both ends).
   - Transition of N bars: linear point at `P` with `bpm_start`, then a point at `P + T_N` with `bpm_end` (shape of the next segment). Beat k of both songs lands at `P + t_k` (formula above), k = 0…4N.
5. **Items:** song i is one item on Deck A (odd i) / Deck B (even i). `POSITION` = start of its first section; `SOFFS` = its source time there; SMs = `(take_pos, src_time)` for every transition beat on both sides plus the solo boundaries (rate between solo boundary SMs is exactly 1.0 because solo is unstretched); `LENGTH` = last SM take position. Fade-in over the whole incoming transition, fade-out over the whole outgoing transition, both shape 1; none on the first song's start or last song's end.
- Output: `Plan(items, tempo_points, transitions, probe_points, phase_points, warnings)`. `probe_points` = every transition SM with its `qn`. `phase_points` = 4 per transition, at beats nearest 1/8, 3/8, 5/8, 7/8 of the transition (never at the fade ends where one deck is silent).
- [ ] Tests: ramp formula vs Simpson integration (1e-9 s); 2/3/5-song layouts (20/36/20 bars); shrink only touches the pair that needs it; caller order preserved exactly; >16% pair error names both songs; SM count `4N+1` per side per transition; solo rate = 1.0 exactly; every transition starts at `Q % 4 == 0`.

### Task 2.3: `rpp_quote.py`
```python
def rpp_quote(text: str, *, is_path: bool) -> str:
    if any(c in text for c in "\r\n\0"):
        raise ReaperMCPError(ErrorCode.INVALID_PARAMETER, f"Line break or NUL not allowed: {text!r}")
    for q in ('"', "'", "`"):
        if q not in text:
            return f"{q}{text}{q}"
    if is_path:
        raise ReaperMCPError(ErrorCode.INVALID_PATH, f"Path contains all three quote characters: {text!r}")
    return '"' + text.replace('"', "'") + '"'
```
Item and track names come from the filename stem via `clean_label()` (strip control characters, collapse whitespace, ≤ 120 chars), so no tag data enters the RPP in v1.
- [ ] Tests: each quote case; CR/LF/NUL rejected; path with all three quotes rejected.

### Task 2.4: `rpp_builder.py`
Header (the sibling's verified recipe): `TIMELOCKMODE 0`, `TEMPOENVLOCKMODE 1`, `DEFPITCHMODE 589824 0`, `SAMPLERATE 44100 0 0`, `TEMPO <first bpm> 4 4 0`, `<TEMPOENVEX>` from `tempo_points` (12 decimals). Two tracks `Deck A`/`Deck B` with `BEAT 0`. Each item: `POSITION`, `LENGTH`, `FADEIN 1 <len> 0 1 0 0 0`, `FADEOUT 1 <len> 0 1 0 0 0`, `MUTE 0 0`, `BEAT 0`, unique `IGUID {uuid4}` and take `GUID {uuid4}` (asserted unique before writing), `NAME "NN. <song>"`, `VOLPAN 1 0 1 -1`, `SOFFS`, `PLAYRATE 1 1 0 -1 5 0.0025`, `SM` (24 pairs per line, 9 decimals), `<SOURCE <WAVE|FLAC|MP3|VORBIS|…>` + `FILE <rpp_quote(path, is_path=True)>`.
GUIDs come from an injectable factory so the golden test is byte-stable.
- [ ] Golden 3-song test; IGUID uniqueness assertion; source tag per extension.

### Task 2.5: `record.py`
Writes `<project>.automix.json`: schema `1`, songs (path, beat source, provenance, `in_bar`), transitions, item IGUID → song order, the planned positions, offsets and SMs, and later the verify report. v1 only writes it; v2's edit sync reads it as the baseline.

---

## Slice 3: Verify, tool, registration

**Files:** `reaper_mcp/automix/verify.py`, `reaper_mcp/tools/automix_tools.py`, `reaper_mcp/tool_registry.py`, `reaper_mcp_shared/constants.py`, `tests/test_automix_verify.py`, `tests/test_automix_tools.py`, `tests/test_profile_allowlists.py`, `scripts/live_probes/{gen_automix_probe.py,automix_gap_probe.lua}`, `docs/TOOLS.md`, `README.md`, `CHANGELOG.md`

### Task 3.1: `verify.py`
- **Grid check:** `automix_grid_probe` in chunks of ≤ 4000 points. Fail if any `|sm_time − grid_time| > 1 µs` or any planned time differs from REAPER's by more than 1 µs.
- **Phase check:** for each phase point, one `automix_phase_envelope` call per transition with Deck A and Deck B windows of ±200 ms around the beat. `best_lag` cross-correlation of the two envelopes within ±80 ms (pure-Python port of `conga_reaper_automix/rb2reaper/verify.py`). `|lag| > 5 ms` → warning, `> 15 ms` → fail, Pearson r < 0.5 → `reliable=false` (reported, not counted).
- Result: `{passed, grid_max_err_us, phase_max_ms, points:[{transition, beat, lag_ms, r, reliable}]}`, also saved into `.automix.json`.
- [ ] Tests: synthetic kick envelopes offset by 0 / 4 / 20 ms → pass / warn / fail; low-correlation noise → unreliable; chunking.

### Task 3.2: `automix_preview` tool
```python
async def automix_preview(songs: list[str], output_dir: str, project_name: str = "preview_minimix") -> dict:
```
Flow: validate → `load_beatmap` for each song (collect **all** errors, then raise one message listing every bad song) → `plan` → write `.rpp` + `.automix.json` (error if either exists) → `project_open(path, new_tab=True)` → `verify` → return the result shown in "What v1 produces".
The docstring states: order is kept exactly, the layout is fixed, beats must come from the sibling analysers (with the command), what `verify.passed=false` means, and that fades are near equal-power.
- [ ] Tests (mocked `ReaperClient`): order preserved; one missing-beats error lists every missing song; existing output → error; relative path → error naming it; the tool calls `project_open` with `new_tab=True` and then the probes; a verify failure is returned, not raised.

### Task 3.3: Registration and docs
- `MAX_AUTOMIX_SONGS = 100`, `MAX_AUTOMIX_PROBE_POINTS = 4000`, `MAX_AUTOMIX_PHASE_WINDOWS = 32` in `constants.py`.
- `"automix_tools"` in `_EXPECTED_MODULES` plus the `mixing` and `production` profiles (`full` includes it automatically); update `test_profile_allowlists.py`.
- No instruction pack (the docstring is enough); no `@undo_grouped` (no undoable changes).
- `docs/TOOLS.md`, `README.md` (counts plus a short "DJ preview minimix" section with the beat prerequisites), `CHANGELOG.md`.

### Task 3.4: Live acceptance (REAPER 7.x, pasted into the PR)
- [ ] **Probe:** port the scratchpad probe to `scripts/live_probes/` (it works in its own tab, saves with option 8, closes with 40860 only when not dirty, and switches back to the original tab). Must pass: grid ≤ 1e-9 s, deck vs grid ≤ 1 ms, A vs B ≤ 1 ms, fade shape 1 = `x(2−x)` ±0.01, action names unchanged.
- [ ] **Real music (the requirement):** `automix_preview` on 5 songs from the library with `snapped` or `aligned` beats → `verify.passed`, every reliable point ≤ 5 ms. Listen to every transition. Run the same 5 songs with `unaligned` `.beats` and record `phase_max_ms`, so the cost of skipping alignment is measured.
- [ ] Order check: the result tracklist, the REAPER item names left to right and the input list match exactly.

---

## Self-Review Notes

- Scope test for any addition: "is it required to turn an ordered list into a beat-synced preview and prove it?" If not, it goes in the roadmap.
- The ramp formula, header values and SM layout are measured facts, so don't redesign them. If a newer REAPER breaks one, the live probe fails; fix the constant, not the test.
- The remaining real-world risk is beat-input quality, and the phase check measures it on every build.
