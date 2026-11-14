"""The scenario rows under other configurations: no UUIDs, features off, odd spools.

Each test is named after the row it runs under another setup: a printer with no tag
reader (MK4, MK3.9), `uuid_field`, `flag_field`, `flag_drift` or `claim_field`
turned off, firmware with no usage counters, spools Spoolman knows little about, and
the corners of what the bridge writes.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace

import pytest

from integration.conftest import (
    CLAIM_FIELD,
    FLAG_FIELD,
    LOAD_MM,
    PETG,
    PLA,
    UUID_FIELD,
    World,
    baseline,
    charges,
    events,
    logged,
    logged_once,
    print_spec,
)
from integration.faults import set_extra_text
from integration.temperatures import TEMPERATURES, WRITTEN
from integration.test_writes import SYNC_FILE, gcode, write_due, wrote_b
from simulator.model import Material
from spoolman_prusalink.printer_sync import PRINTER_MODELS, default_preamble

COREONEL_PREAMBLE = default_preamble(PRINTER_MODELS["8.1.0"])

# Setups that leave the bridge with no instance UUIDs to write or compare: models with no
# tag reader, and a Core One L with the Spoolman field off.
NO_UUIDS = [
    pytest.param("1.4.0", {}, id="mk4"),
    pytest.param("1.3.9", {}, id="mk39"),
    pytest.param("8.1.0", dict(uuid_field=""), id="uuid_field_off"),
]
# The same on the Core One L, whose tag reader the other two keep working.
TAG_READER_NO_UUIDS = NO_UUIDS[2:]
WORLD = "world_model, world_config"


def record_patches(w: World) -> list[dict]:
    """Collect the body of every PATCH the bridge sends Spoolman from now on."""
    seen: list[dict] = []

    async def hook(request):
        if request.method == "PATCH":
            seen.append(json.loads(request.content))
        return None

    w.spoolman_faults.hook = hook
    return seen


def wrote(seen: list[dict], key: str) -> list[dict]:
    """The recorded PATCH bodies that set the extra field `key`."""
    return [body for body in seen if key in body.get("extra", {})]


async def s1_swap_to_b(w: World) -> None:
    """S1 up to the load: B moved in, A unloaded, B active."""
    await baseline(w)
    await w.move("B", w.tool_location)
    await w.poll()
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_committed")) == 1


async def load_tag(w: World, tag: str) -> list[dict]:
    """Load B's PETG from a tag with this UUID, and poll while it settles."""
    records = await w.swap_cycle(
        poll_every=0.3, source="tag", name=PETG[0], color=PETG[1], uuid=tag
    )
    for _ in range(3):
        records += await w.poll()
        w.advance(2.0)
    return records


# --- Printers without UUIDs ---
@pytest.mark.parametrize("stored", [False, True], ids=["new", "stored"])
@pytest.mark.parametrize(WORLD, NO_UUIDS, indirect=True)
async def test_w1_write_without_uuids(world: World, stored: bool):
    """No UUIDs to write: an M865 line with no M865.1, and the spool isn't given a UUID.

    A UUID the spool already has in Spoolman stays there, and isn't written.
    """
    w = world
    b = w.spools["B"]
    theirs = str(uuid.uuid4()) if stored else None
    if stored:
        await set_extra_text(w.spoolman.client, b, UUID_FIELD, theirs)
    await write_due(w)

    await w.poll()
    puts = w.requests("PUT")
    assert len(puts) == 1 and SYNC_FILE in puts[0].text
    await w.run_until(lambda: w.printer.job is None)
    job = w.printer.files[SYNC_FILE]
    assert f'M865 X R N"{PETG[0]}"' in job and f'O"{PETG[1]}"' in job
    assert "M865.1" not in job
    assert not any(t.startswith("M865.1") for t in gcode(w))
    assert await w.spoolman.extra(b, UUID_FIELD) == theirs
    assert w.material() == (PETG[0], PETG[1], None)
    assert w.tool.material.temperatures() == TEMPERATURES

    # The write took with nothing to compare but name, color and temperatures.
    records = await w.poll()
    assert records == []
    assert len(w.requests("PUT")) == 1
    assert await w.flag("B") is None
    assert await w.spoolman.extra(b, UUID_FIELD) == theirs


@pytest.mark.parametrize(WORLD, TAG_READER_NO_UUIDS, indirect=True)
async def test_s1_tag_uuid_is_not_adopted_without_uuids(world: World):
    """UUIDs off, and the new spool's tag has one: nothing is stored, written or flagged."""
    w = world
    b = w.spools["B"]
    await s1_swap_to_b(w)
    tag = str(uuid.uuid4())
    seen = record_patches(w)
    records = await load_tag(w, tag)
    assert w.charged(records, "B") == LOAD_MM
    assert await w.spoolman.extra(b, UUID_FIELD) is None
    assert wrote(seen, UUID_FIELD) == []
    assert w.requests("PUT") == []
    assert w.material() == (PETG[0], PETG[1], tag)
    assert await w.flag("B") is None


# --- Features turned off ---
@pytest.mark.parametrize(
    "world_config, world_printer",
    [({}, dict(flag_drift=False)), (dict(flag_field=""), {})],
    ids=["flag_drift", "flag_field"],
    indirect=True,
)
async def test_w9_drift_is_logged_but_not_flagged_with_flags_off(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Flags off: drift is logged and left alone, and the flag field is never written."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await baseline(w)
    seen = record_patches(w)

    # The color is changed on the printer, with no unload, as the menu's edit would.
    w.tool.material = Material(PLA[0], "#ff0000", w.uuids["A"], **WRITTEN)
    for _ in range(3):
        await w.poll()
        w.advance(1.0)
    assert logged_once(caplog, logging.WARNING, f"printer differs from spool {a}: color")
    assert await w.flag("A") is None
    assert [body for body in seen if FLAG_FIELD in body.get("extra", {})] == []
    assert w.requests("PUT") == []
    assert w.material() == (PLA[0], "#ff0000", w.uuids["A"])


@pytest.mark.parametrize("world_config", [dict(flag_field="")], indirect=True)
async def test_m6_flag_field_off_leaves_every_note(world: World):
    """flag_field off: notes in the field stay on every spool, flagged or not."""
    w = world
    await baseline(w)
    await w.spoolman.ensure_text_field(FLAG_FIELD, "Bridge flag")
    for name in "ABC":
        await set_extra_text(w.spoolman.client, w.spools[name], FLAG_FIELD, f"note on {name}")
    seen = record_patches(w)
    notes = ["note on A", "note on B", "note on C"]

    # S2: the unload would flag A. The note stays, and so does it after Spoolman catches up.
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    assert [await w.flag(name) for name in "ABC"] == notes
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_confirmed")) == 1
    await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    await w.poll()
    assert [await w.flag(name) for name in "ABC"] == notes
    assert wrote(seen, FLAG_FIELD) == []


@pytest.mark.parametrize("world_config", [dict(claim_field="")], indirect=True)
async def test_s5_claim_is_ignored_with_claims_off(world: World, caplog: pytest.LogCaptureFixture):
    """claim_field off: a ticked box moves nothing and the flag says to move usage by hand."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b, c = w.spools["A"], w.spools["B"], w.spools["C"]
    await baseline(w)
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1

    # B goes in at the printer from the menu, Spoolman is forgotten, and a print starts.
    await w.swap_cycle(source="preset", name=PETG[0])
    await w.poll()  # Writes A back over the menu load (W11).
    await w.run_until(lambda: w.printer.job is None)
    records = await w.start_print(print_spec())
    assert len(events(records, "swap_assumed")) == 1
    flag = await w.flag("A") or ""
    assert "charged to it provisionally" in flag
    assert "move the usage in Spoolman by hand" in flag and "tick" not in flag

    # B is moved in and its box ticked, and so is one on C, which is on the shelf.
    seen = record_patches(w)
    await w.move("B", w.tool_location)
    await w.claim("B")
    await w.claim("C")
    mark = w.mark()
    records = await w.poll()
    assert events(records, "claimed") == []
    records += await w.finish_print()
    assert w.charged(w.since(mark), "B") == 0 and w.charged(w.since(mark), "A") > 0
    assert await w.spoolman.extra(b, CLAIM_FIELD) is True
    assert await w.spoolman.extra(c, CLAIM_FIELD) is True
    assert wrote(seen, CLAIM_FIELD) == []
    assert await w.spoolman.location(a) == w.tool_location  # Not evicted.
    assert await w.flag("C") is None
    assert not logged(caplog, logging.WARNING, "is claimed but isn't in a tool's location")

    # B is still staged, so the next print start flags it as not loaded, with no hint to tick.
    await w.start_print(print_spec())
    flag = await w.flag("B") or ""
    assert "staged" in flag and "tick" not in flag


# --- Firmware without usage counters ---
@pytest.mark.parametrize(
    "world_model", ["8.1.0", "2.1.0"], ids=["core_one_l", "mini"], indirect=True
)
async def test_c8_firmware_without_usage_counters(world: World, caplog: pytest.LogCaptureFixture):
    """Firmware with no usage counters: logged once, nothing charged, resumes when they're back."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    w.printer.set_firmware("no_counters")
    mark = w.mark()
    for _ in range(3):
        w.printer.extrude(0, 20)
        await w.poll()
        w.advance(1.0)
    assert w.since(mark) == []
    assert logged_once(caplog, logging.ERROR, "has no usage counters")
    assert await w.used("A") == 0
    assert w.requests("PUT") == []

    # With counters again it starts from a baseline, and charges from there.
    w.printer.set_firmware("full")
    records = await w.poll()
    assert len(events(records, "baseline")) == 1 and charges(records) == []
    assert logged_once(caplog, logging.ERROR, "now reports filament; resuming")
    w.printer.extrude(0, 5)
    # The Core One L counts 5 * 0.95 = 4.75 on top of 1057, three times 20 * 0.95; the MINI
    # feeds 1:1.
    assert w.charged(await w.poll(), "A") == {"8.1.0": 4, "2.1.0": 5}[w.printer.model.code]
    assert logged_once(caplog, logging.ERROR, "has no usage counters")


# --- What the bridge writes ---
# Each case edits B's filament in Spoolman, then B is loaded from the menu and written.
# `present` and `absent` are pieces of the job's M865 line, and `shown` is the name and
# color the printer reports after it. `{short}` is the first seven characters of the
# test's prefix, which is what a filament with no material is named after.
CONTENT = [
    pytest.param(
        dict(material="TPU"),
        ['N"TPU" T220 B60 P210 C25 D15 E40 F1 G1 O"#186bb5" L0'],
        [],
        ("TPU", PETG[1]),
        id="flexible",
    ),
    pytest.param(
        dict(color_hex=None),
        ['N"PETG" T220 B60 P170 C30 D15 E45 L0'],
        [' O"'],
        ("PETG", None),
        id="no_color",
    ),
    pytest.param(
        dict(material=None),
        ['N"{short}" T220 B60 O"#186bb5" L0'],
        [" P1", " C", " G1"],
        ("{short}", PETG[1]),
        id="no_material",
    ),
    pytest.param(
        dict(color_hex="186bb5cc"),
        ['O"#186bb5" L0'],
        ["cc"],
        ("PETG", PETG[1]),
        id="rgba_color",
    ),
]


@pytest.mark.parametrize("edits, present, absent, shown", CONTENT)
async def test_w1_write_content(
    world: World,
    edits: dict,
    present: list[str],
    absent: list[str],
    shown: tuple[str, str | None],
):
    """Flexible, colorless, materialless and RGBA filaments: the M865 line and no false drift."""
    w = world
    b = w.spools["B"]
    short = w.spoolman.prefix[:7]
    await w.spoolman.edit_filament(b, **edits)
    await write_due(w)

    await w.poll()
    puts = w.requests("PUT")
    assert len(puts) == 1 and SYNC_FILE in puts[0].text
    job = w.printer.files[SYNC_FILE]
    line = next(line for line in job.splitlines() if line.startswith("M865 X R"))
    for text in present:
        assert text.format(short=short) in line, line
    for text in absent:
        assert text not in line, line
    await w.run_until(lambda: w.printer.job is None)
    minted = await w.spoolman.extra(b, UUID_FIELD)
    assert minted is not None
    name, color = shown
    assert w.material() == (name.format(short=short), color, minted)

    # The write took, whatever Spoolman holds for the color: no drift, nothing more to write.
    records = await w.poll()
    assert records == []
    assert len(w.requests("PUT")) == 1
    assert await w.flag("B") is None


async def test_w7_spool_with_nothing_to_write_is_flagged(
    world: World, caplog: pytest.LogCaptureFixture
):
    """No material and a name with no usable characters: flagged, written once it has one."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    await w.spoolman.edit_filament(b, material=None, name="!!!", vendor_id=None)
    await write_due(w)

    for _ in range(3):
        await w.poll()
        w.advance(1.0)
    assert w.requests("PUT") == []
    problem = f"spool {b} has no material or name to write"
    assert await w.flag("B") == f"{w.tool_location}: {problem}"
    assert logged_once(
        caplog, logging.WARNING, f"] can't write spool {b} to the printer: {problem}"
    )

    await w.spoolman.edit_filament(b, material="PETG")
    await w.poll()
    assert len(w.requests("PUT")) == 1
    await wrote_b(w)
    await w.poll()
    assert await w.flag("B") is None


async def test_w7_malformed_instance_uuid_is_flagged(
    world: World, caplog: pytest.LogCaptureFixture
):
    """A spool whose instance UUID isn't a UUID: flagged, nothing written until it's fixed."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    await set_extra_text(w.spoolman.client, b, UUID_FIELD, "not-a-uuid")
    await write_due(w)

    for _ in range(3):
        await w.poll()
        w.advance(1.0)
    assert w.requests("PUT") == []
    problem = f"spool {b}'s instance UUID 'not-a-uuid' isn't a UUID; fix or clear it in Spoolman"
    assert await w.flag("B") == f"{w.tool_location}: {problem}"
    assert logged_once(
        caplog, logging.WARNING, f"] can't write spool {b} to the printer: {problem}"
    )
    assert await w.spoolman.extra(b, UUID_FIELD) == "not-a-uuid"  # Left as it was.

    # Cleared, the next write gives the spool a UUID of its own.
    await set_extra_text(w.spoolman.client, b, UUID_FIELD, "")
    await w.poll()
    assert len(w.requests("PUT")) == 1
    await wrote_b(w)
    await w.poll()
    assert await w.flag("B") is None


# The line before the model check, and the model's MMU3 name: both are what the firmware accepts.
PREAMBLES = [
    pytest.param(
        ("G90 ; absolute", 'M862.3 P "COREONEL"', "M862.5 P2 ; level check"), id="lines_before"
    ),
    pytest.param(('M862.3 P "COREONELMMU3"', "M862.5 P2 ; level check"), id="mmu3"),
]


@pytest.mark.parametrize("preamble", PREAMBLES)
async def test_w1_custom_preamble_is_written(world: World, preamble: tuple[str, ...]):
    """A custom preamble with the model's check, or its MMU3 name, starts the job as given."""
    w = world
    assert preamble != COREONEL_PREAMBLE
    await w.restart(printers=(replace(w.cfg.printers[0], gcode_preamble=preamble),))
    await write_due(w)

    await w.poll()
    assert len(w.requests("PUT")) == 1
    lines = w.printer.files[SYNC_FILE].splitlines()
    assert lines[: len(preamble)] == list(preamble)
    assert lines[len(preamble)].startswith("G28 O")
    await wrote_b(w)
    await w.poll()
    assert await w.flag("B") is None
    assert len(w.requests("PUT")) == 1


# --- Drift and edits after a write ---
async def written_b(w: World) -> str:
    """Write B to the printer and let a poll see it take; return the UUID B was written with."""
    await write_due(w)
    await w.poll()
    await wrote_b(w)
    await w.poll()
    assert len(w.requests("PUT")) == 1
    return w.material()[2]


@pytest.mark.parametrize(
    "change, detail",
    [
        pytest.param(dict(name="XYZ"), "name printer='XYZ'", id="name"),
        pytest.param(dict(color="#ff0000"), "color printer=#ff0000", id="color"),
        pytest.param(dict(instance_uuid=str(uuid.uuid4())), "uuid printer=", id="uuid"),
        pytest.param(dict(nozzle=235), "nozzle printer=235", id="temperature"),
    ],
)
async def test_w9_printer_change_beside_an_edit_is_drift(
    world: World, caplog: pytest.LogCaptureFixture, change: dict, detail: str
):
    """A spool edited in Spoolman and the printer changed too: drift, so nothing is written."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    await written_b(w)

    # The edit alone would be written (W14). The printer no longer shows the last write.
    await w.spoolman.edit_filament(b, color_hex="00ad52")
    w.tool.material = replace(w.tool.material, **change)
    for _ in range(3):
        await w.poll()
        w.advance(1.0)
    assert len(w.requests("PUT")) == 1
    assert logged(caplog, logging.WARNING, f"printer differs from spool {b}:")
    assert not logged(caplog, logging.WARNING, "spool changed in Spoolman since the last write")
    flag = await w.flag("B") or ""
    assert "printer filament differs:" in flag and detail in flag


async def test_w14_edit_that_leaves_the_spool_unwritable_is_flagged(
    world: World, caplog: pytest.LogCaptureFixture
):
    """An edit to PPS after a write: flagged with why it can't be written, not drift."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    b = w.spools["B"]
    await written_b(w)

    await w.spoolman.edit_filament(b, material="PPS")
    for _ in range(3):
        await w.poll()
        w.advance(1.0)
    assert len(w.requests("PUT")) == 1
    flag = await w.flag("B") or ""
    assert "needs the printer's HT idler-door setting" in flag
    assert "printer filament differs" not in flag
    assert logged_once(caplog, logging.WARNING, f"] can't write spool {b} to the printer")
    assert not logged(caplog, logging.WARNING, "printer differs from spool")

    # Edited back, the printer matches the spool again and the flag goes.
    await w.spoolman.edit_filament(b, material="PETG")
    await w.poll()
    assert await w.flag("B") is None
    assert len(w.requests("PUT")) == 1


# --- Weights ---
async def drop_weights(w: World, name: str, **filament) -> None:
    """Take a spool's net weight away, as Spoolman allows: its own and its filament's."""
    spool_id = w.spools[name]
    await w.spoolman.edit_filament(spool_id, weight=None, **filament)
    resp = await w.spoolman.client.patch(f"/api/v1/spool/{spool_id}", json={"initial_weight": None})
    resp.raise_for_status()
    spool = await w.spoolman.get(spool_id)
    assert spool.get("initial_weight") is None and spool["filament"].get("weight") is None
    assert spool.get("remaining_weight") is None


async def set_used(w: World, name: str, grams: float) -> None:
    resp = await w.spoolman.client.patch(
        f"/api/v1/spool/{w.spools[name]}", json={"used_weight": grams}
    )
    resp.raise_for_status()


async def test_m5_spool_with_no_weights_and_no_vendor(
    world: World, caplog: pytest.LogCaptureFixture
):
    """No net weight and no vendor: charged and named by the filament, past empty is no flag."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await drop_weights(w, "A", vendor_id=None)
    assert (await w.spoolman.get(a))["filament"].get("vendor") is None
    await baseline(w)

    w.printer.extrude(0, 1000)  # 1000 * 0.95 = 950
    records = await w.poll()
    [charge] = charges(records, a)
    assert charge["use_length"] == 950.0
    assert await w.used("A") == pytest.approx(charge["grams"], abs=0.01) and charge["grams"] > 0
    assert (await w.spoolman.get(a)).get("remaining_weight") is None
    name = f"{w.spoolman.prefix} PLA"  # No vendor's name before it.
    assert logged(caplog, logging.INFO, f"CHARGE spool {a} ({name}): +950 mm")

    # Far past anything the spool could hold: still charged, with no weight to go negative.
    await set_used(w, "A", 5000.0)
    for _ in range(3):
        w.printer.extrude(0, 1000)
        records = await w.poll()
        assert [r["use_length"] for r in charges(records, a)] == [950.0]
    assert await w.used("A") > 5000
    assert not logged(caplog, logging.INFO, f"spool {a} shows -")
    assert await w.flag("A") is None


async def test_m5a_runout_on_a_spool_with_no_net_weight_is_not_flagged(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Runout on a spool with no net weight: unloaded as usual, no flag and no error."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    await drop_weights(w, "A")
    await baseline(w)

    await w.start_print(print_spec())
    w.printer.runout(0)
    records = await w.poll()
    assert len(events(records, "unload")) == 1
    flag = await w.flag("A") or ""  # The unload itself is flagged (S2); the runout isn't.
    assert "unloaded with no Spoolman change" in flag and "unloaded mid-print" not in flag
    assert w.bridge.state.runouts == {}
    assert not logged(caplog, logging.WARNING, "unloaded mid-print")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
