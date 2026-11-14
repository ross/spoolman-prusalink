"""Writing Spoolman's spool to the printer: docs/scenarios.md rows W1 to W15."""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace

import pytest

from integration.conftest import (
    ASA,
    PETG,
    PLA,
    UUID_FIELD,
    World,
    count,
    event_fields,
    events,
    logged,
    logged_once,
    print_spec,
)
from integration.faults import set_extra_text
from integration.temperatures import TEMPERATURES, WRITTEN
from simulator import prusalink
from simulator.model import MODELS, JobPhase, Material, ModelInfo, State
from simulator.prusalink import DROP, Answer, Reply
from simulator.tool import MK4_EXTRUDER
from spoolman_prusalink.printer_sync import PRINTER_MODELS, default_preamble

COREONEL_PREAMBLE = default_preamble(PRINTER_MODELS["8.1.0"])
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

    # Idle and different from B: the bridge uploads the sync job. 30 * 0.95 = 28.5 and the
    # unload's 23 mm ram, 21.85, took the counter to 1050.35; LOADS["8.1.0"], 78.85, on
    # top: 1129.20, so 1129 - 1050 = 79.
    records = await w.poll()
    assert w.charged(records, "B") == 79
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


@pytest.mark.parametrize("world_printer", [dict(sync_delay=5.0)], indirect=True)
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
    await w.poll()
    # The counter moving under the sync job mustn't make it a print start: the name does.
    w.tool.extruded += 5.0
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


async def test_w7_non_preset_without_temperatures_is_flagged(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Material that isn't a preset, no temperatures: flagged, not written until they're set."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    await w.spoolman.edit_filament(
        b, material="PLA-CF", settings_extruder_temp=None, settings_bed_temp=None
    )
    # The printer's menu has no PLA-CF, so the user loads B under another preset.
    await write_due(w)

    for _ in range(3):
        await w.poll()
        w.advance(1.0)
    assert w.requests("PUT") == []
    flag = await w.flag("B")
    assert flag is not None and "isn't a printer preset; set its extruder and bed" in flag
    assert logged_once(caplog, logging.WARNING, f"] can't write spool {b} to the printer")
    assert not logged(caplog, logging.WARNING, f"printer will be set to spool {b}")

    # The flag stays through a print, which is still charged to B.
    records = await w.start_print(print_spec())
    records += await w.run_until(lambda: w.printer.job is None, poll_every=1.0)
    assert w.charged(records, "B") > 0
    assert w.requests("PUT") == []
    assert "set its extruder and bed" in (await w.flag("B") or "")

    # Temperatures set in Spoolman: the next idle poll writes them.
    await w.spoolman.edit_filament(b, settings_extruder_temp=250, settings_bed_temp=80)
    await w.poll()
    puts = w.requests("PUT")
    assert len(puts) == 1 and SYNC_FILE in puts[0].text
    assert f'M865 X R N"PLA-CF" T250 B80 O"{PETG[1]}" L0' in w.printer.files[SYNC_FILE]
    await w.run_until(lambda: w.printer.job is None)
    minted = await w.spoolman.extra(b, "instance_uuid")
    assert w.material() == ("PLA-CF", PETG[1], minted)
    await w.poll()
    assert await w.flag("B") is None
    assert logged_once(caplog, logging.WARNING, f"] can't write spool {b} to the printer")
    # The "can't write" note gave way to a pending write for the same spool, with no
    # end line between; the pending write's note ends once, at its level.
    assert not logged(caplog, logging.WARNING, f"] no longer: can't write spool {b}")
    assert logged_once(caplog, logging.WARNING, f"] no longer: printer will be set to spool {b}")


async def test_w8_pps_is_flagged_until_loaded_from_the_menu(
    world: World, caplog: pytest.LogCaptureFixture
):
    """PPS isn't written but flagged; loaded from the printer's menu, the flag clears."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    await w.spoolman.edit_filament(b, material="PPS")
    # B is loaded from the menu under the wrong preset.
    await write_due(w)

    for _ in range(3):
        await w.poll()
        w.advance(1.0)
    assert w.requests("PUT") == []
    flag = await w.flag("B")
    assert flag is not None and "needs the printer's HT idler-door setting" in flag
    assert logged_once(caplog, logging.WARNING, f"] can't write spool {b} to the printer")

    # The user reloads it from the menu as PPS, as the flag asks. The unload with no
    # Spoolman change opens a swap (W11), but the menu load is what the bridge would
    # have written: no job, and the missing color and UUID aren't drift.
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    await w.swap_cycle(source="preset", name="PPS")
    assert w.material() == ("PPS", None, None)
    for _ in range(3):
        await w.poll()
        w.advance(1.0)
    assert w.requests("PUT") == []
    unloaded = "filament was unloaded with no Spoolman change"
    assert unloaded in (flag := await w.flag("B") or "") and "HT idler-door" not in flag
    assert not logged(caplog, logging.WARNING, f"printer differs from spool {b}")
    assert logged_once(caplog, logging.WARNING, f"] can't write spool {b} to the printer")


@pytest.mark.parametrize("world_printer", [{}, dict(sync_drift=True)], indirect=True)
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
    w.tool.material = Material(PLA[0], ASA[1], w.uuids["A"], **WRITTEN)
    await w.poll()
    if not w.cfg.printers[0].sync_drift:
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


@pytest.mark.parametrize("world_printer", [{}, dict(sync_drift=True)], indirect=True)
async def test_w9_temperature_drift_is_flagged_unless_sync_drift(
    world: World, caplog: pytest.LogCaptureFixture
):
    """A temperature changed on the printer is drift too: flagged, or rewritten."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await w.poll()

    # The nozzle temperature is edited on the printer, with no unload.
    w.tool.material = replace(w.tool.material, nozzle=235)
    await w.poll()
    detail = "nozzle printer=235 spoolman=220"
    if not w.cfg.printers[0].sync_drift:
        assert logged(caplog, logging.WARNING, f"printer differs from spool {a}: {detail}")
        flag = await w.flag("A")
        assert flag is not None and f"printer filament differs: {detail}" in flag
        assert w.requests("PUT") == []
        assert w.tool.material.temperatures() == (235, 170, 60)
        return
    assert len(w.requests("PUT")) == 1
    await w.run_until(lambda: w.printer.job is None)
    assert w.tool.material.temperatures() == TEMPERATURES
    await w.poll()
    assert await w.flag("A") is None


# The charge for the load and three 10 mm extrudes. The unload rams 23 first, from 1000:
# 1021.85. The load's auto-retract rams 23 and leaves the filament 55 mm retracted, so the
# first extrude feeds 15 past the gap: the extrudes count 15 + 30 = 45 mm. The MK4
# extruder loads 30 slow + 50 fast, 40 past its 40 mm gap, and purges 27:
# 1021.85 + (40 + 27 + 23 + 45) * 0.95 = 1150.10, so 1150 - 1021 = 129. The Core One L's
# load is LOADS["8.1.0"]: 1021.85 + 78.85 + 45 * 0.95 = 1143.45, so 1143 - 1021 = 122.
@pytest.mark.parametrize(
    "model, preamble, problem, charged",
    [
        (
            ModelInfo("99.1.0", "FUTURE", MK4_EXTRUDER),
            None,
            "printer type '99.1.0' isn't one the bridge knows",
            129,
        ),
        (
            MODELS["3.1.0"],
            None,
            "XL isn't supported: multi-tool printers aren't supported yet",
            129,
        ),
        (
            MODELS["1.4.0"],
            COREONEL_PREAMBLE,
            "gcode_preamble is for COREONEL but the printer is MK4",
            129,
        ),
        (
            MODELS["8.1.0"],
            ("M862.5 P2 ; level check", 'M862.6 P"Input shaper"'),
            "gcode_preamble has no M862.3 model check; set one for COREONEL",
            122,
        ),
    ],
    ids=["unknown", "unsupported", "mismatched", "no_model_check"],
)
async def test_w10_model_the_bridge_cannot_write(
    world: World,
    caplog: pytest.LogCaptureFixture,
    model: ModelInfo,
    preamble: tuple[str, ...] | None,
    problem: str,
    charged: int,
):
    """Unknown, unsupported or mismatched model: logged once, spool flagged, still charged."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    w.printer.model = model
    if preamble is not None:
        # The preamble is per printer, so it's set by starting the bridge with it.
        await w.restart(printers=(replace(w.cfg.printers[0], gcode_preamble=preamble),))
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    assert logged_once(caplog, logging.WARNING, f"{problem}; printer writes are off")

    # A Spoolman-first swap to B, loaded from the menu: a write is due but can't run.
    await w.move("B", w.tool_location)
    await w.poll()
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_committed")) == 1
    records = await w.swap_cycle(poll_every=0.3, source="preset", name=PETG[0])
    for _ in range(3):
        w.advance(1.0)
        w.printer.extrude(0, 10)
        records += await w.poll()
    assert w.requests("PUT") == []
    assert w.charged(records, "B") == charged
    flag = await w.flag("B")
    assert flag == f"{w.tool_location}: can't write to the printer: {problem}"
    assert logged_once(caplog, logging.WARNING, f"] can't write spool {b} to the printer")
    assert logged_once(caplog, logging.WARNING, "printer writes are off")


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


async def test_w11_tag_is_not_written_back(world: World):
    """Unload with no Spoolman change, then a tag load: A isn't written back over it (Q11).

    The tag's UUID isn't A's, which is drift on A. Moving B in confirms the swap, and B
    takes the tag's UUID with nothing written.
    """
    w = world
    a, b = w.spools["A"], w.spools["B"]
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    tag = str(uuid.uuid4())
    await w.swap_cycle(
        poll_every=0.3,
        source="tag",
        name=PETG[0],
        color="#ff0000",
        uuid=tag,
        temperatures=(250, 170, 80),
    )
    for _ in range(3):
        await w.poll()
        w.advance(2.0)
    assert w.requests("PUT") == []
    assert w.material() == (PETG[0], "#ff0000", tag)
    flag = await w.flag("A") or ""
    assert f"printer filament differs: uuid printer={tag} spoolman={w.uuids['A']}" in flag
    assert await w.spoolman.extra(b, UUID_FIELD) is None

    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    # The unload rammed 23 from 1000: 1021.85. LOADS["8.1.0"], 78.85, on top: 1100.70, so
    # 1100 - 1021 = 79.
    assert event_fields(records, "swap_confirmed") == [{"mm": 79, "from_spool": a, "to_spool": b}]
    for _ in range(2):
        await w.poll()
        w.advance(2.0)
    assert w.requests("PUT") == []
    assert w.material() == (PETG[0], "#ff0000", tag)
    assert await w.spoolman.extra(b, UUID_FIELD) == tag
    assert await w.flag("A") is None
    assert await w.flag("B") is None


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


async def test_w13_sync_job_homes_an_unhomed_printer(world: World):
    """Unhomed after a boot: the sync job homes with G28 O before it writes and ends."""
    w = world
    w.printer.home()
    w.printer.reboot()
    assert not w.printer.homed
    await write_due(w)
    assert not w.printer.homed  # Loading doesn't home.

    await w.poll()
    puts = w.requests("PUT")
    assert len(puts) == 1 and SYNC_FILE in puts[0].text
    lines = w.printer.files[SYNC_FILE].splitlines()
    home = next(i for i, line in enumerate(lines) if line.startswith("G28 O"))
    assert home < next(i for i, line in enumerate(lines) if line.startswith("M865"))
    await wrote_b(w)
    texts = [e.text for e in w.sim.events]
    assert texts.index("G28: homed") < next(
        i for i, t in enumerate(texts) if t.startswith("M865 T0 set to")
    )
    assert texts.index("G28: homed") < next(
        i for i, t in enumerate(texts) if t.startswith("job ") and t.endswith(" finished")
    )


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
    await w.spoolman.edit_filament(b, color_hex="00ad52")
    await w.poll()
    assert logged(caplog, logging.WARNING, "spool changed in Spoolman since the last write")
    assert len(w.requests("PUT")) == 2
    await w.run_until(lambda: w.printer.job is None)
    assert w.material() == (PETG[0], "#00ad52", minted)
    await w.poll()
    assert await w.flag("B") is None

    # A name-only edit: the printer shows the material, so there's nothing to write.
    await w.spoolman.edit_filament(b, name=f"{w.spoolman.prefix} renamed")
    w.advance(1.0)
    await w.poll()
    assert len(w.requests("PUT")) == 2
    assert await w.flag("B") is None


async def test_w14_a_color_the_printer_rounds_is_written_once(
    world: World, caplog: pytest.LogCaptureFixture
):
    """The printer stores 5 bits a channel (F25), so an edit to `00aa55` reads back `00ad52`.

    The bridge compares colors after that rounding, so it sees its write took: no drift and
    no second write.
    """
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    await write_due(w)
    await w.poll()
    await wrote_b(w)
    await w.poll()  # Sees the write took.
    minted = w.material()[2]

    await w.spoolman.edit_filament(b, color_hex="00aa55")
    await w.poll()
    assert len(w.requests("PUT")) == 2
    await w.run_until(lambda: w.printer.job is None)
    assert w.material() == (PETG[0], "#00ad52", minted)
    for _ in range(3):
        await w.poll()
        w.advance(1.0)
    assert len(w.requests("PUT")) == 2
    assert await w.flag("B") is None
    assert not logged(caplog, logging.WARNING, "printer differs from spool")


async def test_w14_edit_after_the_uuid_is_cleared_keeps_the_printers(
    world: World, caplog: pytest.LogCaptureFixture
):
    """UUID cleared in Spoolman, then a color edit: the rewrite keeps the printer's UUID."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    await write_due(w)
    await w.poll()
    await wrote_b(w)
    await w.poll()  # Sees the write took.
    assert len(w.requests("PUT")) == 1
    written = w.material()[2]
    assert logged_once(caplog, logging.INFO, f"gave spool {b} instance UUID")

    # The user clears B's UUID and edits its color: the edit is written with the UUID
    # the printer already shows, and no new one is minted.
    await set_extra_text(w.spoolman.client, b, UUID_FIELD, "")
    assert await w.spoolman.extra(b, UUID_FIELD) is None
    await w.spoolman.edit_filament(b, color_hex="00ad52")
    await w.poll()
    assert len(w.requests("PUT")) == 2
    assert w.bridge.uuids.checked(b, written)  # Checked against archived spools.
    await w.run_until(lambda: w.printer.job is None)
    assert w.material() == (PETG[0], "#00ad52", written)
    m865 = [t for t in gcode(w) if t.startswith("M865.1 T0")]
    assert len(m865) == 2 and written in m865[-1]
    await w.poll()
    assert logged_once(caplog, logging.INFO, f"gave spool {b} instance UUID")
    assert await w.spoolman.extra(b, UUID_FIELD) is None  # Kept on the printer, not stored.
    assert await w.flag("B") is None
    assert len(w.requests("PUT")) == 2


async def test_w3_print_started_before_the_upload(world: World, caplog: pytest.LogCaptureFixture):
    """A print starts between the poll's read and the upload: nothing is uploaded until idle."""
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world
    await write_due(w)

    # The poll reads the printer idle, and a print starts just before the bridge reads
    # the status again to check it's still idle.
    statuses = 0

    def start_print_second(method: str, path: str) -> None:
        nonlocal statuses
        if path == "/api/v1/status":
            statuses += 1
            if statuses == 2:
                w.before_request = None
                w.printer.start_print(print_spec())

    w.before_request = start_print_second
    await w.poll()
    assert statuses == 2
    assert w.printer.state is State.PRINTING
    assert w.requests("PUT") == []
    waits = "printer became PRINTING; sync job waits for the next idle poll"
    assert logged_once(caplog, logging.INFO, waits)

    await w.run_until(lambda: w.printer.job is None, poll_every=1.0)
    assert w.requests("PUT") == []

    # Idle again: the write goes through, and the job's line ends the wait.
    await w.poll()
    puts = w.requests("PUT")
    assert len(puts) == 1 and puts[0].text.endswith(" 201")
    await wrote_b(w)
    await w.poll()
    assert logged_once(caplog, logging.INFO, "became ")
    assert logged_once(caplog, logging.INFO, f"ran printer job {SYNC_FILE}")
    assert count(caplog, logging.WARNING, "sync job") == 0


@pytest.mark.parametrize("failure", ["500", "drop"])
async def test_w15_upload_fails(world: World, caplog: pytest.LogCaptureFixture, failure: str):
    """Upload fails with a 500 or a dropped connection: logged once, retried each idle poll."""
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world
    await write_due(w)

    failed = 0

    def fail_uploads(method: str, path: str) -> Reply:
        nonlocal failed
        if method != "PUT":
            return None
        failed += 1
        if failure == "drop":
            return DROP
        return Answer(500, {"message": "storage error"})

    w.before_request = fail_uploads
    for _ in range(3):
        w.advance(1.0)
        await w.poll()
    assert failed == 3
    assert w.printer.job is None
    assert w.material() == (PETG[0], None, None)
    assert logged_once(caplog, logging.WARNING, "printer sync job failed")

    # The printer takes uploads again: the next idle poll writes B, and says so once.
    w.before_request = None
    await w.poll()
    puts = w.requests("PUT")
    assert puts[-1].text.endswith(" 201")
    await wrote_b(w)
    await w.poll()
    assert logged_once(caplog, logging.WARNING, "printer sync job working again")
    assert logged_once(caplog, logging.WARNING, "printer sync job failed")
    assert logged_once(caplog, logging.INFO, f"ran printer job {SYNC_FILE}")


def mint_refused(w: World) -> None:
    """Make Spoolman refuse to store an instance UUID on spool B until restored."""
    b = w.spools["B"]
    w.spoolman_faults.answer(
        "PATCH", f"/api/v1/spool/{b}", 500, times=None, containing=UUID_FIELD.encode()
    )


async def test_w1_uuid_mint_fails(world: World, caplog: pytest.LogCaptureFixture):
    """Storing a minted UUID fails: logged once, and B's write waits for its UUID (W1)."""
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    mint_refused(w)
    await write_due(w)
    await w.poll()
    await w.run_until(lambda: w.printer.job is None)
    for _ in range(3):
        w.advance(1.0)
        await w.poll()
    assert w.requests("PUT") == []
    assert w.material() == (PETG[0], None, None)
    assert await w.spoolman.extra(b, UUID_FIELD) is None
    assert logged_once(caplog, logging.WARNING, f"can't store an instance UUID on spool {b}")
    assert await w.flag("B") is None


async def test_w1_uuid_minted_once_spoolman_takes_it(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Storing a minted UUID fails, then works: B gets a UUID and the printer shows it (W1)."""
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    mint_refused(w)
    await write_due(w)
    await w.poll()
    await w.run_until(lambda: w.printer.job is None)
    await w.poll()

    w.spoolman_faults.restore()
    for _ in range(3):
        await w.run_until(lambda: w.printer.job is None)
        w.advance(1.0)
        await w.poll()
    minted = await w.spoolman.extra(b, UUID_FIELD)
    assert minted is not None
    assert w.material() == (PETG[0], PETG[1], minted)
    assert logged_once(caplog, logging.WARNING, f"can't store an instance UUID on spool {b}")
    assert logged_once(caplog, logging.INFO, f"gave spool {b} instance UUID")


async def test_w10_model_unreadable_until_it_answers(
    world: World, caplog: pytest.LogCaptureFixture
):
    """`/api/version` fails while other reads work: writes stay off, logged once each way."""
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world

    def no_version(method: str, path: str) -> Reply:
        if path == "/api/version":
            return Answer(500, {"message": "Internal Server Error"})
        return None

    w.before_request = no_version
    await write_due(w)
    records = await w.poll()
    # The counter reads 1129.20: 1000, the 28.5 before the unload, its 23 mm ram, 21.85,
    # and LOADS["8.1.0"], 78.85; so 1129 - 1050 = 79.
    assert w.charged(records, "B") == 79
    # The first 10 mm feed back the load's 55 mm retraction first, 15 past the gap, then
    # 10 each, times 0.95: (15 + 10) * 0.95 = 23.75 to 1152.95, then 1162.45 and 1171.95.
    for charged in (23, 10, 9):
        w.advance(1.0)
        w.printer.extrude(0, 10)
        records = await w.poll()
        assert w.charged(records, "B") == charged
    assert w.requests("PUT") == []
    off = "can't read the printer model ("
    cant = f"] can't write spool {w.spools['B']} to the printer"
    assert logged_once(caplog, logging.WARNING, cant)
    assert logged_once(caplog, logging.WARNING, "printer writes are off until it can")
    flag = await w.flag("B")
    assert flag is not None and f"can't write to the printer: {off}" in flag
    versions = [e for e in w.sim.events if e.kind == "request" and "/api/version" in e.text]
    assert len(versions) == 7  # Read again at each poll until it answers.

    # The printer answers: the model is read once, writes come back on, and B is written.
    w.before_request = None
    await w.poll()
    assert logged_once(caplog, logging.WARNING, "printer is COREONEL (8.1.0)")
    assert len(w.requests("PUT")) == 1
    await wrote_b(w)
    for _ in range(2):
        w.advance(1.0)
        await w.poll()
    assert await w.flag("B") is None
    assert logged_once(caplog, logging.WARNING, "printer writes are off until it can")
    assert count(caplog, logging.INFO, "printer is ") == 0
    assert logged_once(caplog, logging.WARNING, "printer is ")


async def test_w6_sync_job_gone_before_its_name_is_read(
    world: World, caplog: pytest.LogCaptureFixture
):
    """The sync job ends between the bridge's status and job reads: still not a print start."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    await w.swap_cycle(source="preset", name=PETG[0])

    # The idle poll writes A (W11). The next poll's status shows the sync job, but it
    # ends before the bridge asks for the job's name, so `/api/v1/job` answers 204.
    await w.poll()
    assert len(w.requests("PUT")) == 1
    w.advance(0.5)
    assert w.printer.job is not None

    def finish_first(method: str, path: str) -> None:
        if path == "/api/v1/job":
            w.before_request = None
            w.advance(5.0)
            assert w.printer.job is None

    w.before_request = finish_first
    mark = w.mark()
    await w.poll()
    assert [e.text for e in w.requests("GET /api/v1/job")] == ["GET /api/v1/job 204"]
    assert not logged(caplog, logging.WARNING, "read failed")
    # The unload rammed 23 from 1000, 21.85, and the load counted LOADS["8.1.0"], 78.85:
    # 1100.70. The sync job found the filament already retracted, so its end counted
    # nothing. The load left it 55 mm retracted, so the first 5 mm feed 15 past the gap
    # first: (15 + 5 + 5) * 0.95 = 23.75, so 1124.45 and 1124 - 1100 = 24.
    for _ in range(2):
        w.advance(1.0)
        w.printer.extrude(0, 5)
        await w.poll()
    records = w.since(mark)
    assert events(records, "swap_assumed") == []
    assert w.charged(records, "A") == 24
    flag = await w.flag("A")
    assert flag is not None and "unloaded with no Spoolman change" in flag

    # The swap is still open: the user's next print is the print start that assumes A.
    records = await w.start_print(print_spec())
    assert len(events(records, "swap_assumed")) == 1


async def test_w9_malformed_color_is_drift(world: World, caplog: pytest.LogCaptureFixture):
    """The printer reports a color that isn't a hex color: drift, logged and cleared once."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    assert await w.flag("A") is None

    # The simulator only stores valid colors, so the filament reply is edited on the way.
    def bad_color(method: str, path: str) -> Reply:
        if path != "/api/v1/filament":
            return None
        data = prusalink.filament(w.printer)
        assert data is not None
        data["tools"][0]["material"]["color"] = "#7g737b"
        return Answer(200, data)

    # 10 * 0.95 = 9.5 each, from 1000: 1009.5, 1019 and 1028.5.
    w.before_request = bad_color
    for charged in (9, 10, 9):
        w.printer.extrude(0, 10)
        records = await w.poll()
        assert w.charged(records, "A") == charged
    assert w.requests("PUT") == []
    differs = f"printer differs from spool {a}: color printer=#7g737b spoolman={PLA[1]}"
    assert logged_once(caplog, logging.WARNING, differs)
    flag = await w.flag("A")
    assert flag is not None and "color printer=#7g737b" in flag

    # The printer reports a valid color again: the note and the flag end once.
    w.before_request = None
    for _ in range(2):
        await w.poll()
    assert await w.flag("A") is None
    assert logged_once(caplog, logging.WARNING, f"no longer: {differs}")
    assert count(caplog, logging.WARNING, differs) == 2
