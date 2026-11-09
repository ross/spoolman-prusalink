"""Writing Spoolman's spool to the printer: docs/scenarios.md rows W1 to W14."""

from __future__ import annotations

import logging

import pytest

from integration.conftest import ASA, PETG, PLA, World, events, logged, print_spec
from simulator.model import JobPhase, Material, State

SYNC_FILE = "spoolman-sync.gcode"


async def write_due(w: World) -> None:
    """S1 with B loaded from the menu, and no idle poll since: a write of B is due."""
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    await w.move("B", w.tool_location)
    await w.poll()
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_committed")) == 1
    await w.swap_cycle(source="preset", name=PETG[0])
    assert w.material() == (PETG[0], None, None)
    assert w.requests("PUT") == []


async def wrote_b(w: World) -> None:
    """Let the sync job run, then check the printer shows B with its minted UUID."""
    await w.run_until(lambda: w.printer.job is None)
    minted = await w.spoolman.extra(w.spools["B"], "instance_uuid")
    assert minted is not None
    assert w.material() == (PETG[0], PETG[1], minted)


def gcode(w: World) -> list[str]:
    return [e.text for e in w.sim.events if e.kind == "gcode"]


async def test_w1_write_after_swap(world: World):
    """Menu load after a Spoolman-first swap: the bridge writes name, color and UUID."""
    w = world
    b = w.spools["B"]
    records = await w.poll()
    assert len(events(records, "baseline")) == 1

    # S1, with B loaded from the menu: the printer gets the preset's name and no color.
    await w.move("B", w.tool_location)
    await w.poll()
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_committed")) == 1
    # No polls during the load, so the first idle poll below is the one that writes.
    await w.swap_cycle(source="preset", name=PETG[0])
    assert w.material() == (PETG[0], None, None)
    assert w.requests("PUT") == []

    # Idle and different from B: the bridge uploads the sync job.
    records = await w.poll()
    assert w.charged(records, "B") == 36 + 40
    puts = w.requests("PUT")
    assert len(puts) == 1 and SYNC_FILE in puts[0].text
    w.advance(1.5)
    assert w.printer.job is None
    assert any(t.startswith("M865 T0 set to") for t in gcode(w)), gcode(w)
    assert any(t.startswith("M865.1 T0") for t in gcode(w)), gcode(w)
    minted = await w.spoolman.extra(b, "instance_uuid")
    assert minted is not None
    assert w.material() == (PETG[0], PETG[1], minted)

    # The write took: the next poll finds nothing to report or write, and no drift flag.
    records = await w.poll()
    assert records == []
    assert len(w.requests("PUT")) == 1
    assert await w.flag("B") is None


async def test_w2_write_waits_while_printing(world: World):
    """Write due when a print starts: nothing is written until the print ends."""
    w = world
    await write_due(w)

    # A print starts before the bridge's idle poll; polls during it don't write.
    await w.start_print(print_spec())
    w.advance(1.0)
    await w.poll()
    assert w.printer.state is State.PRINTING
    assert w.requests("PUT") == []
    await w.run_until(lambda: w.printer.job is None)
    assert w.requests("PUT") == []

    # Idle again: the first poll writes B.
    await w.poll()
    puts = w.requests("PUT")
    assert len(puts) == 1 and SYNC_FILE in puts[0].text
    await wrote_b(w)


async def test_w3_upload_refused_busy(world: World, caplog: pytest.LogCaptureFixture):
    """Upload refused with 409 when a print starts mid-poll: the next idle poll writes."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    await write_due(w)

    # The poll reads the printer idle, then a print starts just before the upload.
    def start_print_first(method: str, path: str) -> None:
        if method == "PUT":
            w.before_request = None
            w.printer.start_print(print_spec())

    w.before_request = start_print_first
    await w.poll()
    puts = w.requests("PUT")
    assert len(puts) == 1 and puts[0].text.endswith(" 409")
    assert logged(caplog, logging.INFO, "printer busy; sync job waits for the next idle poll")
    assert w.material() == (PETG[0], None, None)

    await w.run_until(lambda: w.printer.job is None, poll_every=1.0)
    assert len(w.requests("PUT")) == 1
    assert w.material() == (PETG[0], None, None)
    assert not any(t.startswith("M865") for t in gcode(w))

    # Idle again: the write goes through.
    await w.poll()
    puts = w.requests("PUT")
    assert len(puts) == 2 and puts[1].text.endswith(" 201")
    await wrote_b(w)


@pytest.mark.parametrize("world_config", [dict(sync_delay=5.0)], indirect=True)
async def test_w4_print_soon_after_loading_runs_first(
    world: World, caplog: pytest.LogCaptureFixture
):
    """With sync_delay, a print started right after a menu load runs before the write."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    await w.move("B", w.tool_location)
    await w.poll()
    await w.unload()
    await w.poll()
    # Polled during the load, so the bridge saw the printer busy until just now.
    await w.swap_cycle(poll_every=0.3, source="preset", name=PETG[0])

    await w.poll()
    assert w.requests("PUT") == []
    msg = "sync job waits until the printer has been idle for 5s"
    assert logged(caplog, logging.INFO, msg)

    # The user starts a print within the delay; it runs with the menu's metadata.
    await w.start_print(print_spec())
    await w.run_until(lambda: w.printer.job is None, poll_every=1.0)
    assert w.requests("PUT") == []
    assert w.material() == (PETG[0], None, None)

    # The delay starts again once the bridge sees the printer idle, then the write runs.
    await w.poll()
    assert w.requests("PUT") == []
    w.advance(5.5)
    await w.poll()
    assert len(w.requests("PUT")) == 1
    await wrote_b(w)


async def test_w5_write_that_does_not_take_is_flagged(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Two sync jobs dropped in a print's start blip: the spool is flagged, no third job."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    await write_due(w)

    # Each poll lands in a print's start blip: the printer reads idle with no job, takes
    # the upload, and drops the print of it.
    for attempt in (1, 2):
        w.printer.start_print(print_spec())
        w.advance(0.3)
        assert w.printer.job.phase is JobPhase.BLIP and w.printer.state is State.IDLE
        await w.poll()
        assert len(w.requests("PUT")) == attempt
        assert w.printer.job.kind == "user"
        await w.run_until(lambda: w.printer.job is None)
    dropped = [e for e in w.sim.events if "the firmware drops it" in e.text]
    assert len(dropped) == 2
    assert w.material() == (PETG[0], None, None)

    await w.poll()
    assert len(w.requests("PUT")) == 2
    flag = await w.flag("B")
    assert flag is not None and "writing this spool to the printer didn't take" in flag
    msg = f"printer still differs from spool {b} after 2 sync jobs"
    assert logged(caplog, logging.WARNING, msg)

    # It stays given up: later idle polls don't write.
    w.advance(1.0)
    await w.poll()
    assert len(w.requests("PUT")) == 2


async def test_w6_sync_job_is_not_a_print_start(world: World):
    """The bridge's sync job, polled while it runs, isn't taken for a print start."""
    w = world
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    await w.swap_cycle(source="preset", name=PETG[0])

    # The idle poll writes A, the assigned spool (W11). Past the job's start blip, the
    # bridge polls it while it runs and looks it up by name.
    mark = w.mark()
    await w.poll()
    assert len(w.requests("PUT")) == 1
    w.advance(0.5)
    assert w.printer.job.phase is JobPhase.RUN and w.printer.state is State.PRINTING
    assert w.requests("GET /api/v1/job") == []
    await w.run_until(lambda: w.printer.job is None, poll_every=0.2)
    records = w.since(mark) + await w.poll()
    assert len(w.requests("GET /api/v1/job")) == 1
    assert len(w.requests("PUT")) == 1
    assert events(records, "swap_assumed") == []
    flag = await w.flag("A")
    assert flag is not None and "unloaded with no Spoolman change" in flag
    assert "provisionally" not in flag

    # The swap is still open: the user's next print is the print start that assumes A.
    records = await w.start_print(print_spec())
    assert len(events(records, "swap_assumed")) == 1


@pytest.mark.parametrize("world_config", [{}, dict(sync_drift=True)], indirect=True)
async def test_w9_drift_is_flagged_unless_sync_drift(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Printer changed with no unload: flagged and left alone, or rewritten with sync_drift."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    records = await w.poll()
    assert len(events(records, "baseline")) == 1

    # The color is changed on the printer, with no unload, as the menu's edit would.
    w.tool.material = Material(PLA[0], ASA[1], w.uuids["A"])
    await w.poll()
    if not w.cfg.sync_drift:
        assert logged(caplog, logging.WARNING, f"printer differs from spool {a}: color")
        flag = await w.flag("A")
        assert flag is not None and "printer filament differs: color" in flag
        assert w.requests("PUT") == []
        assert w.material() == (PLA[0], ASA[1], w.uuids["A"])
        return
    assert len(w.requests("PUT")) == 1
    await w.run_until(lambda: w.printer.job is None)
    assert w.material() == (PLA[0], PLA[1], w.uuids["A"])
    await w.poll()
    assert await w.flag("A") is None


async def test_w11_unconfirmed_swap_writes_the_assigned_spool(world: World):
    """Unload with no Spoolman change, then a menu load: the printer gets A written back."""
    w = world
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    await w.swap_cycle(source="preset", name=PETG[0])
    assert w.material() == (PETG[0], None, None)

    await w.poll()
    puts = w.requests("PUT")
    assert len(puts) == 1 and SYNC_FILE in puts[0].text
    await w.run_until(lambda: w.printer.job is None)
    assert w.material() == (PLA[0], PLA[1], w.uuids["A"])
    assert await w.spoolman.location(w.spools["A"]) == w.tool_location


async def test_w12_staged_spool_waits_for_the_unload(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Write of B due, then C staged: nothing is written until C is unloaded and loaded."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    c = w.spools["C"]
    await write_due(w)

    # Before the idle poll, Spoolman says C replaces B with no unload: C is staged.
    await w.move("B", w.shelf)
    await w.move("C", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_staged")) == 1
    assert logged(
        caplog, logging.WARNING, f"[{w.tool_location}] printer will be set once spool {c} is loaded"
    )
    w.advance(1.0)
    await w.poll()
    assert w.requests("PUT") == []

    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_committed")) == 1
    assert w.requests("PUT") == []
    await w.swap_cycle(source="preset", name=ASA[0])
    await w.poll()
    puts = w.requests("PUT")
    assert len(puts) == 1 and SYNC_FILE in puts[0].text
    await w.run_until(lambda: w.printer.job is None)
    minted = await w.spoolman.extra(c, "instance_uuid")
    assert minted is not None
    assert w.material() == (ASA[0], ASA[1], minted)


async def test_w14_spoolman_edit_is_written(world: World, caplog: pytest.LogCaptureFixture):
    """A color edit in Spoolman is written to the printer; a name-only edit isn't."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    await write_due(w)
    await w.poll()
    await wrote_b(w)
    await w.poll()  # Sees the write took.
    assert len(w.requests("PUT")) == 1
    minted = w.material()[2]

    # A color edit, with the printer still showing the last write: written when idle.
    await w.spoolman.edit_filament(b, color_hex="00aa55")
    await w.poll()
    assert logged(caplog, logging.WARNING, "spool changed in Spoolman since the last write")
    assert len(w.requests("PUT")) == 2
    await w.run_until(lambda: w.printer.job is None)
    assert w.material() == (PETG[0], "#00aa55", minted)
    await w.poll()
    assert await w.flag("B") is None

    # A name-only edit: the printer shows the material, so there's nothing to write.
    await w.spoolman.edit_filament(b, name=f"{w.spoolman.prefix} renamed")
    w.advance(1.0)
    await w.poll()
    assert len(w.requests("PUT")) == 2
    assert await w.flag("B") is None
