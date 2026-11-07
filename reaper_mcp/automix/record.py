"""The `<project>.automix.json` record written next to every preview project.

It stores what was planned (positions, offsets, stretch markers) and, once
verified, the sync report. v1 only writes it; a later edit-sync feature reads
it as the baseline to diff REAPER edits against.
"""

from __future__ import annotations

import json
from pathlib import Path

from .planner import Plan

SCHEMA = 1


def record_dict(plan: Plan, item_guids: dict[int, str], project: str) -> dict:
    return {
        "schema": SCHEMA,
        "project": project,
        "songs": [
            {
                "order": it.order, "path": it.audio, "name": it.name, "deck": it.deck,
                "beat_source": it.beat_source, "provenance": it.provenance, "in_bar": it.in_bar,
                "bpm": it.bpm, "item_guid": item_guids[it.order],
                "position": it.position, "soffs": it.soffs, "length": it.length,
                "fade_in": it.fade_in, "fade_out": it.fade_out,
                "stretch_markers": [[take, src] for take, src in it.sms],
            }
            for it in plan.items
        ],
        "transitions": [
            {
                "from": t.from_order, "to": t.to_order, "bars": t.bars,
                "bpm_start": t.bpm_start, "bpm_end": t.bpm_end, "stretch_pct": t.stretch_pct,
                "start_time": t.start_time, "end_time": t.end_time, "start_qn": t.start_qn,
            }
            for t in plan.transitions
        ],
        "tempo_points": [{"time": p.time, "bpm": p.bpm, "shape": p.shape} for p in plan.tempo_points],
        "warnings": list(plan.warnings),
        "verify": None,
    }


def write_record(path: Path, data: dict) -> None:
    """Create the record. Fails if the file already exists."""
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def save_verify(path: Path, report: dict) -> None:
    """Store the verify report in an existing record."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data["verify"] = report
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
