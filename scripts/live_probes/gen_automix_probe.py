"""Generate the automix live probe: test media, a probe .rpp, and a Lua script for REAPER.

    python scripts/live_probes/gen_automix_probe.py [--out DIR] [--scripts-dir DIR]

Then, in REAPER, run Actions > "ReaScript: Run" on the generated
``automix_gap_probe.lua`` (or run it with ``script_run``). It works in its own
project tab and returns to yours. Read ``<out>/result.json``; the ExtState
``reaper_mcp_script_result/last_result`` says PASS or FAIL.

The project is two decks of synthetic kicks (120 and 130 BPM) stretched per beat
onto one linear 120 -> 130 BPM ramp, which is exactly what automix_preview builds,
so the probe checks the REAPER behaviour the planner relies on (see the plan's
"Facts" table).
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path

SR = 44100
RAMP_BEATS = 16
B0, B1 = 120.0, 130.0
HERE = Path(__file__).resolve().parent


def forward(path: Path | str) -> str:
    return str(path).replace("\\", "/")


def default_scripts_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "REAPER"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "REAPER"
    else:
        base = Path.home() / ".config" / "REAPER"
    return base / "Scripts" / "ReaperMCP"


def write_wav(path: Path, samples: list[float]) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(struct.pack("<h", max(-32767, min(32767, int(s * 32767)))) for s in samples))


def kicks(bpm: float, seconds: float) -> list[float]:
    x = [0.0] * int(seconds * SR)
    period = 60.0 / bpm
    t = 0.0
    while t < seconds - 0.1:
        start = int(round(t * SR))
        phase = 0.0
        for i in range(int(0.08 * SR)):
            freq = 50 + 130 * math.exp(-i / (0.01 * SR))
            phase += 2 * math.pi * freq / SR
            x[start + i] += 0.8 * math.exp(-i / (0.015 * SR)) * math.sin(phase)
        t += period
    return x


def ramp_times() -> tuple[float, list[float]]:
    """Seconds of each beat on a linear B0 -> B1 ramp over RAMP_BEATS beats (the planner's formula)."""
    duration = RAMP_BEATS * 60.0 / ((B0 + B1) / 2)
    slope = (B1 - B0) / duration
    times = [(-B0 + math.sqrt(B0 * B0 + 120.0 * slope * k)) / slope if k else 0.0 for k in range(RAMP_BEATS + 1)]
    return duration, times


def sm_lines(pairs: list[tuple[float, float]]) -> list[str]:
    return [
        "      SM " + " + ".join(f"{p:.9f} {s:.9f}" for p, s in pairs[i:i + 24])
        for i in range(0, len(pairs), 24)
    ]


def item(pos, length, soffs, path, name, guid_n, sms=(), fadein=None) -> list[str]:
    fade_in = f"      FADEIN {fadein[0]} {fadein[1]:.9f} 0 1 0 0 0" if fadein else "      FADEIN 0 0 0 1 0 0 0"
    return [
        "    <ITEM",
        f"      POSITION {pos:.9f}",
        "      SNAPOFFS 0",
        f"      LENGTH {length:.9f}",
        "      LOOP 0",
        "      ALLTAKES 0",
        fade_in,
        "      FADEOUT 0 0 0 1 0 0 0",
        "      MUTE 0 0",
        "      BEAT 0",
        "      SEL 0",
        "      IGUID {00000000-0000-0000-0000-%012d}" % guid_n,
        f'      NAME "{name}"',
        "      VOLPAN 1 0 1 -1",
        f"      SOFFS {soffs:.9f}",
        "      PLAYRATE 1 1 0 -1 5 0.0025",
        "      CHANMODE 0",
        "      GUID {AAAAAAAA-0000-0000-0000-%012d}" % guid_n,
        *sm_lines(list(sms)),
        "      <SOURCE WAVE",
        f'        FILE "{path}"',
        "      >",
        "    >",
    ]


def track(n: int, name: str, items: list[list[str]]) -> list[str]:
    return [
        "  <TRACK {BBBBBBBB-0000-0000-0000-%012d}" % n,
        f'    NAME "{name}"',
        "    BEAT 0",
        "    VOLPAN 1 0 -1 -1 1",
        "    NCHAN 2",
        *[line for it in items for line in it],
        "  >",
    ]


def build(out: Path, scripts_dir: Path) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    k120, k130, dc = out / "kicks120.wav", out / "kicks130.wav", out / "dc it's.wav"  # the apostrophe tests quoting
    write_wav(k120, kicks(120, 40))
    write_wav(k130, kicks(130, 40))
    write_wav(dc, [0.5] * (SR * 10))

    duration, tk = ramp_times()
    sm_a = [(tk[k], 2.0 + 0.5 * k) for k in range(RAMP_BEATS + 1)]
    b_soffs = 2 * 60.0 / 130.0                     # lands on a kick
    sm_b = [(tk[k], b_soffs + (60.0 / 130.0) * k) for k in range(RAMP_BEATS + 1)]

    rpp = [
        '<REAPER_PROJECT 0.1 "7.0/win64" 1700000000',
        "  TIMELOCKMODE 0",
        "  TEMPOENVLOCKMODE 1",
        "  DEFPITCHMODE 589824 0",
        "  SAMPLERATE 44100 0 0",
        "  TEMPO 120 4 4 0",
        "  <TEMPOENVEX",
        "    EGUID {CCCCCCCC-0000-0000-0000-000000000001}",
        "    ACT 1 -1",
        "    VIS 1 0 1",
        "    LANEHEIGHT 0 0",
        "    ARM 0",
        "    DEFSHAPE 1 -1 -1",
        '    PT 0.000000000000 120.0000000000 0 262148 0 1 0 "" 0 169 0 ABBB',
        f"    PT {duration:.12f} 130.0000000000 1",
        "  >",
        *track(1, "Deck A", [item(0.0, duration, 2.0, forward(k120), "DeckA", 1, sm_a)]),
        *track(2, "Deck B", [item(0.0, duration, b_soffs, forward(k130), "DeckB", 2, sm_b)]),
        *track(3, "Ref", [item(10.0, 5.0, 0.0, forward(k120), "Ref", 3)]),
        *track(4, "Fade1", [item(20.0, 6.0, 0.0, forward(dc), "Fade1", 4, fadein=(1, 4.0))]),
        ">",
    ]
    rpp_path = out / "probe.rpp"
    rpp_path.write_text("\n".join(rpp) + "\n", encoding="utf-8")

    template = (HERE / "automix_gap_probe.lua").read_text(encoding="utf-8")
    lua = (
        template.replace("@@OUT@@", forward(out / "result.json"))
        .replace("@@RPP@@", forward(rpp_path))
        .replace("@@SAVE@@", forward(out / "probe_saved.rpp"))
        .replace("@@TK@@", ",".join(f"{t:.12f}" for t in tk))
        .replace("@@T@@", f"{duration:.12f}")
    )
    scripts_dir.mkdir(parents=True, exist_ok=True)
    lua_path = scripts_dir / "automix_gap_probe.lua"
    lua_path.write_text(lua, encoding="utf-8")
    return rpp_path, lua_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "reaper_mcp_automix_probe")
    parser.add_argument("--scripts-dir", type=Path, default=default_scripts_dir())
    args = parser.parse_args()
    rpp_path, lua_path = build(args.out, args.scripts_dir)
    print(f"probe project: {rpp_path}")
    print(f"probe script:  {lua_path}")
    print(f"result:        {args.out / 'result.json'}  (also ExtState reaper_mcp_script_result/last_result)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
