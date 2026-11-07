# DJ Automix: Roadmap after v1

v1 ([`2026-11-06-reaper-native-dj-automix.md`](2026-11-06-reaper-native-dj-automix.md)) does one thing: an ordered list of songs becomes a verified, beat-synced preview minimix in REAPER. Everything else from the original plan and its audit is here, grouped into versions. Each version gets its own implementation plan when it's picked up; the notes below keep the audit findings (A-numbers) and measured REAPER behaviour so they aren't rediscovered.

Measured facts referenced below come from REAPER 7.79/x64 on 2026-11-06 (see the v1 plan's facts table and `scripts/live_probes/`).

---

## v2: Full mix and edits from the preview

**Features**
- `automix_full_mix(session_path, …)`: Stage 2, built from v1's `.automix.json`. Configurable `default_transition_bars` (4–64, multiple of 4), `solo_bars` or `max_solo_seconds` (rounded down to whole 8-bar phrases), full song up to the last clean 8-bar boundary, `fade_shape` (`linear`/`equal_power`/`s_curve`/`cut`), per-track `gain_db`, per-track overrides (`in_bar`, `out_bar`, `transition_bars`, `drop`).
- Configurable transition/solo/fade on `automix_preview` too.
- Edit sync: changes the user makes to the preview in REAPER (delete, mute, reorder, slip, volume) carry into Stage 2.
- `_plan.csv` export/import.
- `overwrite=True` for outputs.

**Bridge work (reuse, don't duplicate: A7, A8)**
- `project_list_tabs` (read-only `EnumProjects` loop: index, path, active, dirty) and `project_activate_tab(path)` (`SelectProjectInstance`; exempt).
- `item_get_all(include_edit_state=true)` extends `build_item_info` with `item_guid`, `D_STARTOFFS`, take `D_VOL` and stretch markers (capped at 2048, plus a truncation flag). Default off, so existing callers see no change.

**Rules carried over from the audit**
- **Live read only (A11):** find the preview by path → activate the tab → read. No Python RPP parser. If it isn't open, there are no REAPER edits; say so.
- **Match by IGUID (A13, A25):** v1 already writes unique IGUIDs in `.automix.json`.
- **Layered diff against the baseline (A14):** REAPER diff → CSV cells that differ from the baseline CSV → overrides → tool args. A later layer wins; disagreements are listed in `conflicts`. Never apply the whole CSV (that reverts REAPER edits).
- **Slip vs trim, from measured behaviour (A12):** REAPER moves SM *take positions* to keep content in place and never changes `src_pos`. Measured: slip +0.25 s → offset −0.25, SM positions +0.25, position unchanged. Left trim +0.5 s → position +0.5, length −0.5, offset +0.5, SM positions −0.5.
  `content_shift = Δ(pos + sm_pos)` for the baseline's first SM (matched by `src_pos`), `left_edge_shift = Δpos`:
  - shift ≈ edge shift ≠ 0 → moved (reorder only)
  - shift ≠ 0, edge ≈ 0 → slip → convert to source beats → nearest downbeat → new `in_bar`
  - shift ≈ 0, edge ≠ 0 → trim → `ignored_edits`
  - only the right edge moved → `ignored_edits`
  - SMs edited by hand → warning, fall back to `D_STARTOFFS`
  - unknown IGUID with the same source → split, keep the leftmost piece
  - item `D_VOL` → `gain_db`; take `D_VOL` ≠ 1 → warning
- **Never write `D_STARTOFFS` on SM items (A26):** the API leaves the SMs where they were. Slip with `ApplyNudge(proj, 0, 4, 1, Δ, false, 0)`.
- **Fades (A21):** `linear→0` (−3 dB dip), `equal_power→1` (`x(2−x)`, +0.51 dB, documented as "near"), `s_curve→5` (smoothstep), `cut→` zero-length fade. **Never 6** (steep S-curve, not a cut).
- Overwrite: an output open in any REAPER tab is always an error (A24).

---

## v3: Library input and ordering

**Features**
- Inputs beyond an explicit list: `.m3u/.m3u8` (UTF-8 with or without BOM, falling back to Latin-1; `file://` URIs; relative entries resolved against the playlist), Rekordbox `.txt` (UTF-16/UTF-8 TSV `Location`, else one path per line), `.csv`, `.toml` (`tomllib`→`tomli` fallback: A17), `.automix.json`, directory scan (`MAX_AUTOMIX_SCAN_FILES`).
- Tags: mutagen (`TIT2/TPE1/TKEY`, Vorbis `INITIALKEY`, MP4 `initialkey`) → `track.json` → filename. Every tag string goes through `clean_label()` before reaching an RPP (A15). New extra: `automix = ["mutagen>=1.47", "tomli>=2.0; python_version < '3.11'"]`. Mutagen tests use `importorskip` because CI installs only `.[dev,analysis]`.
- Camelot parser and distance (`1A–12B`, `Am`, `F#m`, Open Key).
- Ordering: `greedy_2opt` (renamed from "greedy_anneal", which had no annealing: A23), `curated_dp` (BPM sorted by `(bpm, path)` so the graph stays acyclic, exact DP over `count`, `max_bpm_step_pct`, `max_camelot_distance`, `song_margin`), `keep_order` (v1's behaviour).
- Unknown keys (A16): `unknown_key_policy="allow"|"exclude"` (default allow). Always report `excluded`.
- Half/double time (A4): `allow_half_double_bpm`. SM mapping **strides**: at factor 2, source beat j → grid beat 2j with N even; at factor ½, source beat 2j → grid beat j. Downbeats must land on grid downbeats. The sibling's port only folded the BPM, which is a 2× stretch bug.
- `force_large_stretch` to allow pairs above 16% (A22).
- Drift reporting (A5): a `tempo_stability` warning when the grid in solos is visibly off (cues stay correct because they're in source bars).

**Reuse (A10):** move `AUDIO_EXTS`, `_iter_audio_files` (safe walk with `prune_unsafe_subdirs`), `_parse_bpm`, `_parse_key` and `_duration_seconds` from `loops_tools.py` into `reaper_mcp_shared/audio_files.py`, keeping aliases.

---

## v4: Render and delivery

**Features**
- `automix_render(session_path, output_audio_path, format, sample_rate, channels, write_tracklist)`.
- Ruler lanes: Songs regions, Transitions regions, `#NN Title` cue markers, `=START` / `=END`. Measured: the RPP lane field is 1-based (1/2/3) while `I_LANENUMBER` reads back 0-based; `=END` sets `GetProjectLength`.
- `.txt` tracklist and `.cue` built from the **live** markers (`marker_get_all`), so user edits are included.

**Bridge work (reuse: A9)**
- Extend `project_export_audio` with `bounds="custom"` (`RENDER_BOUNDSFLAG=0`, `RENDER_STARTPOS/ENDPOS`), `sample_rate` (`RENDER_SRATE`), `channels` (`RENDER_CHANNELS`) and `expected_project_path`, which errors before writing any setting if the active tab isn't the mix (A20). All keys were measured to write and read back.
- Render bounds come from the `=START`/`=END` marker positions, set explicitly rather than relying on "entire project".
- Refuse to render when the last verify for that `.rpp` failed (`require_verified=True`).

---

## v5: Sound

- Bass-swap EQ automation (ReaEQ low shelf, automation items), as in the sibling's rekordbox path.
- Loudness matching (`gain_db` toward the set median, clamped), LUFS master.
- Pre-roll / mix-in offsets, short transitions on key clashes.

---

## Later / unscheduled

- CD image render (REAPER's `#` marker convention).
- Non-4/4 meters; variable-tempo songs inside transitions.
- Phase rotation and anchor overrides per song (automixer_mcp has `phase_rotation`).

## Never in this repo

- **Beat analysis** (beat_this, torch, onset snapping) and `generate_missing_beats` (A19). It stays in `conga_automixer` / `automixer_mcp`. Reaper-MCP only reads their output, as CLAUDE.md requires for separate services.

## Housekeeping (any time)

- `CLAUDE.md` profile list is missing `production`.
- Existing `project_open` freezes on a dirty project; fixed in v1 Slice 1.
