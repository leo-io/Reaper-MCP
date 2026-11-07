"""DJ preview minimix: an ordered list of songs becomes a beat-synced REAPER project."""

import os
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from reaper_mcp.automix import record
from reaper_mcp.automix.beatmap import load_beatmap
from reaper_mcp.automix.planner import plan as make_plan
from reaper_mcp.automix.rpp_builder import build_rpp
from reaper_mcp.automix.verify import verify
from reaper_mcp_shared.constants import MAX_AUTOMIX_SONGS
from reaper_mcp_shared.error_codes import ErrorCode, ReaperMCPError
from reaper_mcp_shared.path_safety import safe_path

AUDIO_EXTENSIONS = (".wav", ".flac", ".mp3", ".aif", ".aiff", ".ogg", ".m4a")
# 1-80 of letters, digits, space, _ . -  - but never starting or ending with a dot or space
# (a name like ".." or "x." misbehaves on Windows).
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9_-]([A-Za-z0-9 _.-]{0,78}[A-Za-z0-9_-])?$")


def _validate_songs(songs) -> list[str]:
    if not isinstance(songs, list) or not 2 <= len(songs) <= MAX_AUTOMIX_SONGS:
        count = len(songs) if isinstance(songs, list) else type(songs).__name__
        raise ReaperMCPError(
            ErrorCode.INVALID_PARAMETER,
            f"songs must be a list of 2-{MAX_AUTOMIX_SONGS} audio file paths, got {count}",
        )
    resolved, problems = [], []
    for n, song in enumerate(songs, 1):
        try:
            if not isinstance(song, str):
                raise ReaperMCPError(ErrorCode.INVALID_PARAMETER, f"not a path string: {song!r}")
            path = safe_path(song)
            if Path(path).suffix.lower() not in AUDIO_EXTENSIONS:
                raise ReaperMCPError(
                    ErrorCode.INVALID_FORMAT,
                    f"unsupported audio type {Path(path).suffix!r} (use {', '.join(AUDIO_EXTENSIONS)})",
                )
            if not os.path.isfile(path):
                raise ReaperMCPError(ErrorCode.INVALID_PATH, "file does not exist")
            resolved.append(path)
        except ReaperMCPError as exc:
            problems.append(f"songs[{n}] {song!r}: {exc.message}")
    if problems:
        raise ReaperMCPError(ErrorCode.INVALID_PARAMETER, "Bad songs: " + "; ".join(problems))
    return resolved


def _validate_output(output_dir, project_name) -> tuple[Path, Path]:
    if not isinstance(output_dir, str):
        raise ReaperMCPError(ErrorCode.INVALID_PATH, f"output_dir must be a path string, got {output_dir!r}")
    directory = safe_path(output_dir)
    if not os.path.isdir(directory):
        raise ReaperMCPError(ErrorCode.INVALID_PATH, f"output_dir does not exist: {output_dir!r}")
    if not isinstance(project_name, str) or not _PROJECT_NAME.match(project_name):
        raise ReaperMCPError(
            ErrorCode.INVALID_PARAMETER,
            f"project_name must be 1-80 letters, digits, spaces, '_', '.' or '-' (starting and ending with a "
            f"letter, digit, '_' or '-'), got {project_name!r}",
        )
    rpp = Path(directory) / f"{project_name}.rpp"
    rec = Path(directory) / f"{project_name}.automix.json"
    existing = [str(p) for p in (rpp, rec) if p.exists()]
    if existing:
        raise ReaperMCPError(
            ErrorCode.INVALID_PARAMETER,
            f"Output already exists: {', '.join(existing)}. Choose another project_name.",
        )
    return rpp, rec


def _load_all_beatmaps(paths: list[str]):
    maps, problems = [], []
    for n, path in enumerate(paths, 1):
        try:
            maps.append(load_beatmap(path))
        except ReaperMCPError as exc:
            problems.append(f"songs[{n}]: {exc.message}")
    if problems:
        raise ReaperMCPError(
            ErrorCode.INVALID_PARAMETER,
            f"{len(problems)} of {len(paths)} songs have no usable beats:\n" + "\n".join(problems),
        )
    return maps


async def build_preview(client, songs, output_dir, project_name) -> dict:
    """The whole automix_preview flow, with the REAPER client passed in."""
    paths = _validate_songs(songs)
    rpp_path, record_path = _validate_output(output_dir, project_name)
    maps = _load_all_beatmaps(paths)
    the_plan = make_plan(maps)
    built = build_rpp(the_plan)

    try:
        with open(rpp_path, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(built.text)
        record.write_record(record_path, record.record_dict(the_plan, built.item_guids, str(rpp_path)))
    except OSError as exc:
        rpp_path.unlink(missing_ok=True)
        raise ReaperMCPError(ErrorCode.COMMAND_FAILED, f"Could not write the project files: {exc}") from exc

    opened = await client.execute("project_open", path=str(rpp_path), new_tab=True)
    warnings = list(the_plan.warnings) + list(opened.get("data", opened).get("warnings") or [])

    try:
        report = await verify(client, the_plan, built.item_guids)
    except ReaperMCPError as exc:
        report = {
            "passed": False, "grid_max_err_us": None, "phase_max_ms": None, "points": [],
            "failures": [f"verify could not run: {exc.message}"], "failure_count": 1, "warnings": [],
        }
    try:
        record.save_verify(record_path, report)
    except OSError as exc:
        warnings.append(f"could not save the verify report to {record_path}: {exc}")

    return {
        "project": str(rpp_path),
        "record": str(record_path),
        "tracklist": [
            {"order": it.order, "song": it.name, "bpm": round(it.bpm, 2), "beats": it.provenance,
             "in_bar": it.in_bar, "deck": it.deck}
            for it in the_plan.items
        ],
        "transitions": [
            {"from": t.from_order, "to": t.to_order, "bars": t.bars, "bpm_start": round(t.bpm_start, 2),
             "bpm_end": round(t.bpm_end, 2), "stretch_pct": round(t.stretch_pct, 2)}
            for t in the_plan.transitions
        ],
        "warnings": warnings,
        "verify": report,
    }


def register(mcp: FastMCP):
    from reaper_mcp.main import client

    @mcp.tool()
    async def automix_preview(songs: list[str], output_dir: str, project_name: str = "preview_minimix") -> dict:
        """Build a beat-synced DJ preview minimix of your songs and open it in a new REAPER tab.

        The songs are mixed IN EXACTLY THE ORDER GIVEN. Nothing is reordered or
        dropped; a song that cannot be placed is an error naming that song.

        Layout (fixed): 4 solo bars per song, 16-bar transitions between
        songs. A transition shrinks to 8 or 4 bars only for a pair where one
        song has no clean stretch long enough. Songs alternate between tracks
        "Deck A" and "Deck B". In a transition both decks are stretched, one
        beat at a time, onto a single linear tempo ramp from the outgoing
        song's tempo to the incoming one's, with near-equal-power item fades
        (about +0.5 dB at the crossover). Solo bars play at normal speed.
        Tempo jumps over 6% add a warning; over 16% are an error.

        BEAT FILES ARE REQUIRED. This tool does not analyse audio. Each song
        needs beats from one of these (an error lists every song that has none):
          - `automixer beatthis <song folder>` (conga_automixer; writes
            beat_this/<name>.beats), or
          - the snapped beatmaps of automixer_mcp
            (.automixer_cache/<name>_<hash>.beatmap.json).
        Result `beats` per song: snapped / aligned = trustworthy; unaligned /
        unknown = may be off by up to 20 ms (a warning is added).

        The result always includes a sync report in `verify`, measured in
        REAPER after the project opens:
          - grid: every transition stretch marker vs REAPER's tempo map
            (`grid_max_err_us`; must be within 1 microsecond);
          - phase: the two decks' audio cross-correlated at 4 beats of each
            transition (`phase_max_ms`; warning above 5 ms, failure above 15 ms;
            points with `reliable: false` had no shared beat and don't count).
        `verify.passed` is false when a check fails or could not run; the
        project is still open and the report says what failed. It is never
        hidden. Listen to the transitions: passing proves timing, not taste.

        Writes `<project_name>.rpp` and `<project_name>.automix.json` (a
        record of the plan and the report) in `output_dir`. Existing files are
        never overwritten; choose another project_name.

        Args:
            songs: 2 to 100 absolute paths to audio files (.wav .flac .mp3
                .aif .aiff .ogg .m4a), in the order they should play.
            output_dir: Absolute path of an existing folder for the output.
            project_name: File name without extension: letters, digits,
                spaces, '_', '.' and '-' (max 80 characters; must start and
                end with a letter, digit, '_' or '-').
        """
        return await build_preview(client, songs, output_dir, project_name)
