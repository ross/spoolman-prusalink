"""Replays of the printer behaviors in docs/printer-behaviors.md against traces from the printer.

Each replay performs one behavior in the simulator while sampling it the way the probe
samples a real printer, then checks the simulator's signature against the trace recorded
live under `traces/`. A behavior whose trace hasn't been captured yet is skipped, and the
skip says which file to capture. `python -m simulator.trace traces/F2-unload.jsonl` prints
a trace's steps when a replay fails. `test_plumbing_traces.py` tests the format, the
signature and the comparison on their own, and thins these same replays the way the
probe would to check that a correct simulator passes at any polling interval.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from simulator import prusalink, trace
from simulator.model import (
    JobPhase,
    Material,
    Power,
    PrintSpec,
    Segment,
    SimPrinter,
    SimTool,
    Simulation,
    State,
)
from simulator.tests.util import make
from spoolman_prusalink import printer_sync
from spoolman_prusalink.models import Spool

TRACES = Path(__file__).parent / "traces"
STEP = 0.1
LEAD = 2.5
PETG = Material("PETG", None, None)
TAGGED = Material(
    "PETG", "#186bb5", "7b3d2d5e-1f7a-4d8c-9a1e-2c3b4d5e6f70", nozzle=230, preheat=170, bed=85
)
# The bridge's job for TAGGED's spool with no temperatures in Spoolman, which writes the
# PETG preset's, built by the bridge so a change to its job shows up here.
SYNC_JOB = printer_sync.build_job(
    printer_sync.default_preamble(printer_sync.PRINTER_MODELS["8.1.0"]),
    [
        printer_sync.ToolWrite(
            0,
            Spool(2, "Core One L+", "PETG", "PETG", TAGGED.color[1:], 1.75, 1.27),
            TAGGED.instance_uuid,
        )
    ],
)

# Timing that keeps a replay to a few hundred ticks while every phase still lasts several
# samples, as it does on the printer. Each replay starts with `LEAD` seconds at rest,
# longer than any probe interval the plumbing tests try, so a trace polled from any
# offset begins at rest, as docs/printer-behaviors.md asks of a recording. The scan
# keeps the model's default: how long the printer takes to scan the sync job's four
# lines isn't recorded yet, and the comparison lets a poll miss it either way.
SETTINGS = dict(
    unload_seconds=2.0,
    insert_seconds=2.0,
    continue_seconds=2.0,
    purge_park_seconds=1.0,
    color_seconds=2.0,
    load_rate_mm_s=5.0,
    blip_seconds=0.3,
    park_seconds=4.0,
    sync_job_seconds=2.0,
    home_seconds=3.0,
    preheat_seconds=2.0,
    print_rate_mm_s=20.0,
    change_park_seconds=3.0,
    change_unload_seconds=3.0,
    change_return_seconds=8.0,
    end_park_seconds=5.0,
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
                trace.Sample(self.t, prusalink.status(p), prusalink.filament(p), prusalink.job(p))
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
        self.until(lambda: all(t.process is None for t in self.printer.tools.values()), limit=120.0)

    def ended(self) -> None:
        """Until the job is over."""
        self.until(lambda: self.printer.job is None)

    def reset(self) -> None:
        """Start the recording over from the printer's current state, for a replay that
        begins somewhere other than idle with the default spool."""
        self.samples.clear()
        self.t = 0.0
        self.look()

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
    problems = trace.compare(live, sim, recorded.header.get("interval"))
    assert not problems, "\n".join(
        problems + ["", "live:", trace.describe(live), "", "simulator:", trace.describe(sim)]
    )


def recorder() -> Recorder:
    sim, printer = make(**SETTINGS)
    # The traced Core One L has the high-flow nozzle its build sets by default
    # (`nozzle_is_high_flow` in `defaults.hpp`), which doubles a load's purge.
    printer.high_flow = True
    return Recorder(sim, printer)


# The replays. Each follows the steps for its row in docs/printer-behaviors.md.


def f1_reboot(rec: Recorder) -> None:
    rec.advance(LEAD)
    rec.printer.set_power("off")
    rec.advance(3.0)
    rec.printer.set_power("on")
    rec.advance(2.0)


def f2_unload(rec: Recorder) -> None:
    rec.advance(LEAD)
    rec.printer.unload(0)
    rec.settled()
    rec.advance(2.0)


def f3_load_preset(rec: Recorder) -> None:
    f2_unload(rec)
    rec.printer.load(0, source="preset", name=PETG.name)
    rec.settled()
    rec.advance(2.0)


def f4_load_tag(rec: Recorder) -> None:
    f2_unload(rec)
    m = TAGGED
    rec.printer.load(0, source="tag", name=m.name, color=m.color, uuid=m.instance_uuid)
    rec.settled()
    rec.advance(2.0)


def _print(rec: Recorder, mm: float = 200.0, **kw) -> None:
    rec.printer.start_print(PrintSpec([Segment(0, mm)], **kw))


def _printing(rec: Recorder, mm: float = 20.0) -> None:
    """Until the print has fed `mm` more."""
    extruded = rec.printer.tools[0].extruded
    rec.until(lambda: rec.printer.tools[0].extruded > extruded + mm)


def f5_print(rec: Recorder) -> None:
    rec.advance(LEAD)
    _print(rec)
    rec.ended()
    rec.advance(2.0)
    rec.printer.dismiss()
    rec.advance(2.0)


def _mid_print(rec: Recorder) -> None:
    """Start the recording partway through a print, as a trace that begins `PRINTING` does,
    so the replay leaves the print's start out. The print feeds about 1 mm/s, slowly
    enough that polls see the counter both still and moving, as the printer's did."""
    _print(rec, 2000.0, duration_s=2000.0)
    rec.until(lambda: rec.printer.job.phase is JobPhase.PRINTING, limit=120.0)
    _printing(rec, 3.0)
    rec.reset()
    _printing(rec, 40.0)
    rec.advance(0.5)  # between two of the counter's steps


def f6_pause_resume(rec: Recorder) -> None:
    """A pause and a resume from the screen, from `PRINTING` to `PRINTING`."""
    _mid_print(rec)
    rec.printer.pause()
    rec.advance(15.0)
    rec.printer.resume()
    _printing(rec, 15.0)


def f7_cancel(rec: Recorder) -> None:
    """Stop from the screen, then the stopped screen dismissed."""
    _mid_print(rec)
    rec.printer.cancel()
    rec.ended()
    rec.advance(2.0)
    rec.printer.dismiss()
    rec.advance(2.0)


def _change_filament(rec: Recorder) -> None:
    """A filament change during a print, from its dialog to `PRINTING`: the unload leaves
    the filament in the sensor, the user pulls it out and answers that it came out, and
    reloads the same spool; the print goes on by itself after the color question."""
    rec.until(lambda: rec.printer.tools[0].path.at_sensor)  # unloaded, still in the sensor
    rec.advance(2.0)
    rec.printer.pull(0)
    rec.advance(5.0)
    rec.printer.load(0)  # The reload keeps the type the printer had.
    rec.until(lambda: rec.printer.state is State.PRINTING)
    _printing(rec, 15.0)


def f8_change_filament(rec: Recorder) -> None:
    """Tune > Change filament, `M600 P`, on the print's tool."""
    _mid_print(rec)
    rec.printer.change_filament(0)
    _change_filament(rec)


def f9_m600(rec: Recorder) -> None:
    """A color change in the file. The trace begins idle, so the replay runs the start."""
    rec.advance(LEAD)
    _print(rec, 2000.0, duration_s=2000.0, color_changes_mm=[60.0])
    rec.until(lambda: rec.printer.state is State.ATTENTION, limit=120.0)
    _change_filament(rec)


def f10_sync_job(rec: Recorder) -> None:
    rec.advance(LEAD)
    rec.printer.upload("spoolman-sync.gcode", SYNC_JOB, print_after=True, overwrite=True)
    rec.ended()
    rec.advance(2.0)


def _auto_retracted(tool: SimTool) -> None:
    """Leave a Core One L tool as its last auto-retract left it: 55 mm stored, the tracker
    at its 40 mm limit and invalid. The F10 traces start from such a tool: their counter
    stayed 0 through a sync job, which auto-retracts a tool that isn't."""
    tool.path.auto_retracted, tool.path.gap, tool.path.gap_valid = 55.0, 40.0, False


def f10b_sync_job_rewrite(rec: Recorder) -> None:
    """The bridge's job written by hand with the spool already loaded, from the FINISHED
    screen: the slot's values don't change, so no material event."""
    tool = rec.printer.tools[0]
    tool.fill(TAGGED)
    _auto_retracted(tool)
    rec.printer.screen = State.FINISHED
    rec.printer.homed = True  # the earlier sync job homed it
    rec.reset()
    rec.advance(LEAD)
    rec.printer.upload("spoolman-sync.gcode", SYNC_JOB, print_after=True, overwrite=True)
    rec.ended()
    rec.advance(2.0)


def f10c_sync_job_rewrite_idle(rec: Recorder) -> None:
    """As F10b, but from IDLE after a boot, with Z not homed: `G28 O` homes first, so the
    job runs much longer on the printer, which the comparison doesn't time."""
    tool = rec.printer.tools[0]
    tool.fill(TAGGED)
    _auto_retracted(tool)
    rec.printer.homed = False
    rec.reset()
    rec.advance(LEAD)
    rec.printer.upload("spoolman-sync.gcode", SYNC_JOB, print_after=True, overwrite=True)
    rec.ended()
    rec.advance(2.0)


def f11_manual_extrude(rec: Recorder) -> None:
    rec.advance(LEAD)
    rec.printer.extrude(0, 50)
    rec.advance(2.0)
    rec.printer.retract(0, 10)
    rec.advance(2.0)
    rec.printer.extrude(0, 50)
    rec.advance(2.0)


def _power_cut(rec: Recorder, seconds: float, **on) -> None:
    rec.advance(LEAD)
    _print(rec)
    _printing(rec)
    rec.printer.set_power("off")
    rec.advance(seconds)
    rec.printer.set_power("on", resume=True, **on)


def f12_power_panic(rec: Recorder) -> None:
    """A short cut: the bed is still warm, so the print resumes on its own."""
    _power_cut(rec, 5.0)
    rec.ended()
    rec.advance(2.0)


def f13_power_panic_cold(rec: Recorder) -> None:
    """A long cut: the printer waits for the user before resuming."""
    _power_cut(rec, 5.0, cold_bed=True)
    rec.until(lambda: rec.printer.state is State.ATTENTION)
    rec.advance(3.0)
    rec.printer.resume()
    rec.ended()
    rec.advance(2.0)


def _from_empty(rec: Recorder) -> None:
    """Start from an empty tool with no type, as a hand pull leaves it."""
    rec.printer.tools[0].fill(None)
    rec.reset()


def _start_at_screen(rec: Recorder, material: str, **spec) -> None:
    """Start a print on the empty tool and wait at the preview's warning that it has no
    filament. The printer reads `IDLE` with no job while its preview is open, which the
    simulator doesn't model; it shows the scan instead, which the probe can miss."""
    rec.printer.start_print(PrintSpec([Segment(0, 200, material)], **spec))
    rec.until(lambda: rec.printer.state is State.ATTENTION)
    rec.advance(2.0)
    rec.printer.tap_through()  # Print: on to the filament-not-inserted screen
    rec.advance(2.0)


def f28_warning_abort(rec: Recorder) -> None:
    """An empty tool, a print started at the printer, and its warning, where the trace
    stops: the job reads `ATTENTION` with no `PRINTING` sample before it."""
    _from_empty(rec)
    rec.advance(LEAD)
    rec.printer.start_print(PrintSpec([Segment(0, 200, "TESTPET")]))
    rec.until(lambda: rec.printer.state is State.ATTENTION)
    rec.advance(3.0)


def f28_load_then_decline(rec: Recorder) -> None:
    """The preview's load on an empty tool, then Abort at the wrong-filament question, then
    the bridge's sync job. The file's type, `TESTPETG`, is cut to 7 characters, a name the
    build doesn't know, so the load asks for a type and PETG is picked."""
    _from_empty(rec)
    rec.advance(LEAD)
    _start_at_screen(rec, "TESTPET")
    rec.printer.load(0, source="preset", name=PETG.name)  # Yes, then PETG
    rec.until(lambda: rec.printer.state is State.ATTENTION)  # the wrong-filament question
    rec.advance(2.0)
    rec.printer.cancel()  # Abort
    rec.advance(2.0)
    rec.printer.upload("spoolman-sync.gcode", SYNC_JOB, print_after=True, overwrite=True)
    rec.ended()
    rec.advance(2.0)


def f29_sensors_off_print(rec: Recorder) -> None:
    """A menu unload and a pull, a print on the empty tool past Disable FS, a cancel, a menu
    load and the bridge's sync job with the sensors off, a hand pull nothing sees, and the
    sensors back on."""
    tool = rec.printer.tools[0]
    tool.fill(TAGGED)
    _auto_retracted(tool)  # as the sync job before the trace left it
    rec.reset()
    rec.advance(LEAD)
    rec.printer.unload(0)
    rec.settled()
    rec.advance(2.0)
    rec.printer.pull(0)  # the menu unload left the filament in the sensor
    rec.advance(2.0)
    _start_at_screen(rec, PETG.name, duration_s=400.0, preheat_s=10.0)
    # The printer read `ATTENTION` with the job for 13 s after Disable FS had turned the
    # sensors off, which nothing in the source explains, so the replay turns them off first.
    rec.printer.set_sensors(False)
    rec.advance(2.0)
    rec.printer.tap_through()  # Disable FS
    # Slow enough that polls see the counter both still and moving, as the printer's did.
    # The menu unload left the tracker's gap at its limit, which the print refills first.
    rec.until(lambda: rec.printer.tools[0].extruded > 25.0, limit=300.0)
    rec.printer.cancel()
    rec.ended()
    rec.advance(2.0)
    rec.printer.dismiss()
    rec.advance(2.0)
    rec.printer.load(0, source="preset", name=PETG.name)
    rec.settled()
    rec.advance(2.0)
    rec.printer.upload("spoolman-sync.gcode", SYNC_JOB, print_after=True, overwrite=True)
    rec.ended()
    rec.advance(2.0)
    rec.printer.dismiss()
    rec.advance(2.0)
    rec.printer.pull(0)  # with the sensors off, nothing reads it
    rec.advance(2.0)
    rec.printer.set_sensors(True)
    rec.advance(2.0)


def f47_sensors_setting(rec: Recorder) -> None:
    """The sensors turned off and on from the menu, filament loaded and idle."""
    rec.advance(LEAD)
    rec.printer.set_sensors(False)
    rec.advance(3.0)
    rec.printer.set_sensors(True)
    rec.advance(2.0)


def f48_unload_load(rec: Recorder) -> None:
    """A menu unload, the filament pulled out of the sensor, then a menu load, as F3."""
    rec.advance(LEAD)
    rec.printer.unload(0)
    rec.settled()
    rec.advance(2.0)
    rec.printer.pull(0)
    rec.advance(2.0)
    rec.printer.load(0, source="preset", name=PETG.name)
    rec.settled()
    rec.advance(2.0)


def f48_hand_pull(rec: Recorder) -> None:
    """Loaded filament pulled out of a cold extruder by hand, idle, with the sensors on."""
    rec.advance(LEAD)
    rec.printer.pull(0)
    rec.advance(2.0)


REPLAYS: dict[str, Callable[[Recorder], None]] = {
    "F1-reboot": f1_reboot,
    "F2-unload": f2_unload,
    "F3-load-preset": f3_load_preset,
    "F4-load-tag": f4_load_tag,
    "F5-print": f5_print,
    "F6-pause-resume": f6_pause_resume,
    "F7-cancel": f7_cancel,
    "F8-change-filament": f8_change_filament,
    "F9-m600": f9_m600,
    "F10-sync-job": f10_sync_job,
    "F10b-sync-job-rewrite": f10b_sync_job_rewrite,
    "F10c-sync-job-rewrite-idle": f10c_sync_job_rewrite_idle,
    "F11-manual-extrude": f11_manual_extrude,
    "F12-power-panic": f12_power_panic,
    "F13-power-panic-cold": f13_power_panic_cold,
    "F28-warning-abort": f28_warning_abort,
    "F28-load-then-decline": f28_load_then_decline,
    "F29-sensors-off-print": f29_sensors_off_print,
    "F47-sensors-setting": f47_sensors_setting,
    "F48-unload-load": f48_unload_load,
    "F48-hand-pull": f48_hand_pull,
}


@pytest.mark.parametrize("name", REPLAYS)
def test_replay(name: str):
    rec = recorder()
    REPLAYS[name](rec)
    check(name, rec)
