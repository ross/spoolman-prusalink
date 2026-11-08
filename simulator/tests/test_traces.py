"""Replays of the printer behaviors in docs/printer-behaviors.md against traces from the printer.

Each `test_f*` performs one behavior in the simulator while sampling it the way the probe
samples a real printer, then checks the simulator's signature against the trace recorded
live under `traces/`. A behavior whose trace hasn't been captured yet is skipped, and the
skip says which file to capture. `python -m simulator.trace traces/F2-unload.jsonl` prints
a trace's steps when a replay fails. `test_plumbing_traces.py` tests the format, the
signature and the comparison on their own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from simulator import trace
from simulator.model import Material, Power, PrintSpec, Segment, SimPrinter, Simulation, State
from simulator.tests.util import make

TRACES = Path(__file__).parent / "traces"
STEP = 0.1
PETG = Material("PETG", None, None)
TAGGED = Material("PETG", "#1f6fb2", "7b3d2d5e-1f7a-4d8c-9a1e-2c3b4d5e6f70")
SYNC_JOB = (
    'M862.3 P "COREONEL"\nG28 O\nM865 X R N"PETG" O"#1f6fb2" L0\n'
    f'M865.1 I0 C"{TAGGED.instance_uuid}"\n'
)

# Timing that keeps a replay to a few hundred ticks while every phase still lasts several
# samples, as it does on the printer.
SETTINGS = dict(
    unload_seconds=2.0,
    load_rate_mm_s=20.0,
    scan_seconds=0.5,
    blip_seconds=0.3,
    park_seconds=1.0,
    sync_job_seconds=2.0,
    preheat_seconds=2.0,
    print_rate_mm_s=20.0,
)


class Recorder:
    """Samples a simulated printer after every tick, as the probe samples a real one."""

    def __init__(self, sim: Simulation, printer: SimPrinter) -> None:
        self.sim, self.printer = sim, printer
        self.samples: list[trace.Sample] = []
        self.t = 0.0
        self.look()

    def look(self) -> None:
        p = self.printer
        if p.power is not Power.ON:
            self.samples.append(trace.Sample(self.t, error="connection refused"))
        else:
            self.samples.append(
                trace.Sample(self.t, p.status_json(), p.filament_json(), p.job_json())
            )

    def advance(self, seconds: float) -> None:
        t = 0.0
        while t < seconds - 1e-9:
            dt = min(STEP, seconds - t)
            self.sim.tick(dt)
            self.t += dt
            t += dt
            self.look()

    def until(self, done, limit: float = 60.0) -> None:
        start = self.t
        while not done():
            assert self.t - start < limit, f"not done after {limit} simulated seconds"
            self.advance(STEP)

    def settled(self) -> None:
        """Until no load or unload is running."""
        self.until(lambda: all(t.process is None for t in self.printer.tools.values()))

    def ended(self) -> None:
        """Until the job is over."""
        self.until(lambda: self.printer.job is None)

    def steps(self) -> list[trace.Step]:
        return trace.signature(self.samples)


def check(name: str, rec: Recorder) -> None:
    """Compare the replay with the live trace `traces/<name>.jsonl`, or skip without one."""
    path = TRACES / f"{name}.jsonl"
    if not path.exists():
        pytest.skip(f"no live trace yet: record {path.name} as docs/printer-behaviors.md says")
    recorded = trace.read(path)
    live = trace.signature(recorded.samples, recorded.header.get("tool", 0))
    sim = rec.steps()
    problems = trace.compare(live, sim)
    assert not problems, "\n".join(
        problems + ["", "live:", trace.describe(live), "", "simulator:", trace.describe(sim)]
    )


@pytest.fixture
def rec() -> Recorder:
    sim, printer = make(**SETTINGS)
    return Recorder(sim, printer)


# The replays. Each follows the steps for its row in docs/printer-behaviors.md.


def test_f1_reboot(rec: Recorder):
    rec.advance(1.0)
    rec.printer.set_power("off")
    rec.advance(3.0)
    rec.printer.set_power("on")
    rec.advance(2.0)
    check("F1-reboot", rec)


def test_f2_unload(rec: Recorder):
    rec.advance(1.0)
    rec.printer.unload(0)
    rec.settled()
    rec.advance(1.0)
    check("F2-unload", rec)


def test_f3_load_preset(rec: Recorder):
    rec.printer.unload(0)
    rec.settled()
    rec.advance(1.0)
    rec.printer.load(0, source="preset", name=PETG.name)
    rec.settled()
    rec.advance(1.0)
    check("F3-load-preset", rec)


def test_f4_load_tag(rec: Recorder):
    rec.printer.unload(0)
    rec.settled()
    rec.advance(1.0)
    m = TAGGED
    rec.printer.load(0, source="tag", name=m.name, color=m.color, uuid=m.instance_uuid)
    rec.settled()
    rec.advance(1.0)
    check("F4-load-tag", rec)


def _print(rec: Recorder, mm: float = 200.0, **kw) -> None:
    rec.printer.start_print(PrintSpec([Segment(0, mm)], **kw))


def test_f5_print(rec: Recorder):
    rec.advance(1.0)
    _print(rec)
    rec.ended()
    rec.advance(1.0)
    rec.printer.dismiss()
    rec.advance(1.0)
    check("F5-print", rec)


def test_f6_pause_resume(rec: Recorder):
    rec.advance(1.0)
    _print(rec)
    extruded = rec.printer.tools[0].extruded
    rec.until(lambda: rec.printer.tools[0].extruded > extruded + 20)
    rec.printer.pause()
    rec.advance(3.0)
    rec.printer.resume()
    rec.ended()
    rec.advance(1.0)
    check("F6-pause-resume", rec)


def test_f7_cancel(rec: Recorder):
    rec.advance(1.0)
    _print(rec)
    extruded = rec.printer.tools[0].extruded
    rec.until(lambda: rec.printer.tools[0].extruded > extruded + 20)
    rec.printer.cancel()
    rec.ended()
    rec.advance(1.0)
    check("F7-cancel", rec)


def _change_filament(rec: Recorder, name: str) -> None:
    """A filament change during a print: the tool runs out or the user asks for one."""
    rec.until(lambda: rec.printer.state is State.ATTENTION)
    rec.advance(2.0)
    rec.printer.load(0)  # The reload keeps the type the printer had.
    rec.until(lambda: rec.printer.state is State.PAUSED)
    rec.advance(1.0)
    rec.printer.resume()
    rec.ended()
    rec.advance(1.0)
    check(name, rec)


def test_f8_change_filament(rec: Recorder):
    rec.advance(1.0)
    _print(rec)
    extruded = rec.printer.tools[0].extruded
    rec.until(lambda: rec.printer.tools[0].extruded > extruded + 20)
    rec.printer.runout(0)
    _change_filament(rec, "F8-change-filament")


def test_f9_m600(rec: Recorder):
    rec.advance(1.0)
    _print(rec, color_changes_mm=[60.0])
    _change_filament(rec, "F9-m600")


def test_f10_sync_job(rec: Recorder):
    rec.advance(1.0)
    rec.printer.upload("spoolman-sync.gcode", SYNC_JOB, print_after=True, overwrite=True)
    rec.ended()
    rec.advance(1.0)
    check("F10-sync-job", rec)


def test_f11_manual_extrude(rec: Recorder):
    rec.advance(1.0)
    rec.printer.extrude(0, 50)
    rec.advance(1.0)
    rec.printer.retract(0, 10)
    rec.advance(1.0)
    rec.printer.extrude(0, 50)
    rec.advance(1.0)
    check("F11-manual-extrude", rec)


def _power_cut(rec: Recorder, seconds: float, **on) -> None:
    rec.advance(1.0)
    _print(rec)
    extruded = rec.printer.tools[0].extruded
    rec.until(lambda: rec.printer.tools[0].extruded > extruded + 20)
    rec.printer.set_power("off")
    rec.advance(seconds)
    rec.printer.set_power("on", resume=True, **on)


def test_f12_power_panic(rec: Recorder):
    """A short cut: the bed is still warm, so the print resumes on its own."""
    _power_cut(rec, 5.0)
    rec.ended()
    rec.advance(1.0)
    check("F12-power-panic", rec)


def test_f13_power_panic_cold_bed(rec: Recorder):
    """A long cut: the printer waits for the user before resuming."""
    _power_cut(rec, 5.0, cold_bed=True)
    rec.until(lambda: rec.printer.state is State.ATTENTION)
    rec.advance(3.0)
    rec.printer.resume()
    rec.ended()
    rec.advance(1.0)
    check("F13-power-panic-cold", rec)
