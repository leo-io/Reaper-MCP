"""Write a Plan as a REAPER project (.rpp): a tempo map and two deck tracks.

The header values, stretch-marker layout and item fields follow the recipe
verified in REAPER 7.79 (see the plan's "Facts" table): per-beat `SM` with
`PLAYRATE ... 5 0.0025` (no pre-echo reduction, 2.5 ms fade), élastique 3.3.3
Pro as the default pitch mode, and a time timebase on items and tracks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from reaper_mcp_shared.error_codes import ErrorCode, ReaperMCPError

from .planner import FADE_SHAPE, ItemPlan, Plan, TempoPoint
from .rpp_quote import rpp_quote

SAMPLE_RATE = 44100
SM_PAIRS_PER_LINE = 24
# REAPER's own tag for each container; an unlisted extension falls back to WAVE.
SOURCE_TAGS = {
    ".wav": "WAVE", ".aif": "WAVE", ".aiff": "WAVE", ".flac": "FLAC",
    ".mp3": "MP3", ".ogg": "VORBIS", ".m4a": "WAVE",
}
_FIRST_TEMPO_POINT_EXTRA = '262148 0 1 0 "" 0 169 0 ABBB'


def new_guid() -> str:
    return "{" + str(uuid.uuid4()).upper() + "}"


@dataclass
class BuildResult:
    text: str
    item_guids: dict[int, str]      # song order -> item GUID (IGUID)


def _source_tag(path: str) -> str:
    return SOURCE_TAGS.get(Path(path).suffix.lower(), "WAVE")


def _tempo_lines(points: list[TempoPoint], guid: Callable[[], str]) -> list[str]:
    lines = [
        "  <TEMPOENVEX",
        f"    EGUID {guid()}",
        "    ACT 1 -1",
        "    VIS 1 0 1",
        "    LANEHEIGHT 0 0",
        "    ARM 0",
        "    DEFSHAPE 1 -1 -1",
    ]
    for n, pt in enumerate(points):
        extra = f" {_FIRST_TEMPO_POINT_EXTRA}" if n == 0 else ""
        lines.append(f"    PT {pt.time:.12f} {pt.bpm:.10f} {pt.shape}{extra}")
    lines.append("  >")
    return lines


def _sm_lines(sms: list[tuple[float, float]]) -> list[str]:
    lines = []
    for start in range(0, len(sms), SM_PAIRS_PER_LINE):
        chunk = sms[start:start + SM_PAIRS_PER_LINE]
        lines.append("      SM " + " + ".join(f"{take:.9f} {src:.9f}" for take, src in chunk))
    return lines


def _item_lines(item: ItemPlan, iguid: str, take_guid: str) -> list[str]:
    label = f"{item.order:02d}. {item.name}"
    return [
        "    <ITEM",
        f"      POSITION {item.position:.9f}",
        "      SNAPOFFS 0",
        f"      LENGTH {item.length:.9f}",
        "      LOOP 0",
        "      ALLTAKES 0",
        f"      FADEIN {FADE_SHAPE} {item.fade_in:.9f} 0 1 0 0 0",
        f"      FADEOUT {FADE_SHAPE} {item.fade_out:.9f} 0 1 0 0 0",
        "      MUTE 0 0",
        "      BEAT 0",
        "      SEL 0",
        f"      IGUID {iguid}",
        f"      IID {item.order}",
        f"      NAME {rpp_quote(label, is_path=False)}",
        "      VOLPAN 1 0 1 -1",
        f"      SOFFS {item.soffs:.9f}",
        "      PLAYRATE 1 1 0 -1 5 0.0025",
        "      CHANMODE 0",
        f"      GUID {take_guid}",
        *_sm_lines(item.sms),
        f"      <SOURCE {_source_tag(item.audio)}",
        f"        FILE {rpp_quote(item.audio, is_path=True)}",
        "      >",
        "    >",
    ]


def _track_lines(name: str, peakcol: int, items: list[list[str]], guid: Callable[[], str]) -> list[str]:
    tid = guid()
    lines = [
        f"  <TRACK {tid}",
        f"    NAME {rpp_quote(name, is_path=False)}",
        f"    PEAKCOL {peakcol}",
        "    BEAT 0",
        "    AUTOMODE 0",
        "    PANLAWFLAGS 3",
        "    VOLPAN 1 0 -1 -1 1",
        "    MUTESOLO 0 0 0",
        "    IPHASE 0",
        "    PLAYOFFS 0 1",
        "    ISBUS 0 0",
        "    BUSCOMP 0 0 0 0 0",
        "    SHOWINMIX 1 0.6667 0.5 1 0.5 0 0 0 0",
        "    FIXEDLANES 9 0 0 0 0",
        "    SEL 0",
        "    REC 0 0 1 0 0 0 0 0",
        "    VU 64",
        "    TRACKHEIGHT 0 0 0 0 0 0 0",
        "    INQ 0 0 0 0.5 100 0 0 100",
        "    NCHAN 2",
        "    FX 1",
        f"    TRACKID {tid}",
        "    PERF 0",
        "    MIDIOUT -1 -1",
        "    MAINSEND 1 0",
    ]
    for chunk in items:
        lines.extend(chunk)
    lines.append("  >")
    return lines


def build_rpp(plan: Plan, guid: Callable[[], str] = new_guid) -> BuildResult:
    """The project text for `plan`. `guid` is injectable so tests are byte-stable."""
    item_guids: dict[int, str] = {}
    take_guids: dict[int, str] = {}
    seen: set[str] = set()
    for item in plan.items:
        item_guids[item.order] = guid()
        take_guids[item.order] = guid()
    for g in (*item_guids.values(), *take_guids.values()):
        if g in seen:
            raise ReaperMCPError(ErrorCode.COMMAND_FAILED, f"Duplicate GUID generated for the project: {g}")
        seen.add(g)

    deck_items: dict[str, list[list[str]]] = {"A": [], "B": []}
    for item in plan.items:
        deck_items[item.deck].append(_item_lines(item, item_guids[item.order], take_guids[item.order]))

    first_bpm = plan.tempo_points[0].bpm
    lines = [
        '<REAPER_PROJECT 0.1 "7.79/win64" 1794006276 0',
        "  RIPPLE 0 0",
        "  GROUPOVERRIDE 0 0 0 0",
        "  AUTOXFADE 132",
        "  ENVATTACH 3",
        "  MIXERUIFLAGS 11 48 0",
        "  PEAKGAIN 1",
        "  PANLAW 1",
        "  GRID 3199 8 1 8 1 0 0 0",
        "  TIMEMODE 1 5 -1 30 0 0 -1 0",
        "  PANMODE 3",
        "  PANLAWFLAGS 3",
        "  TIMELOCKMODE 0",
        "  TEMPOENVLOCKMODE 1",
        "  ITEMMIX 1",
        "  DEFPITCHMODE 589824 0",
        f"  SAMPLERATE {SAMPLE_RATE} 0 0",
        f"  TEMPO {first_bpm:.10f} 4 4 0",
        "  PLAYRATE 1 0 0.25 4",
        "  MASTER_NCH 2 2",
        "  MASTER_VOLUME 1 0 -1 -1 1",
        "  MASTER_PANMODE 3",
        "  MASTER_PANLAWFLAGS 3",
        "  MASTER_FX 1",
        *_tempo_lines(plan.tempo_points, guid),
        "  <PROJBAY",
        "  >",
        *_track_lines("Deck A", 29917766, deck_items["A"], guid),
        *_track_lines("Deck B", 19429596, deck_items["B"], guid),
        ">",
    ]
    return BuildResult("\n".join(lines) + "\n", item_guids)
