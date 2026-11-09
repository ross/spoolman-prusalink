"""Spool swaps and claims: docs/scenarios.md rows S1 to S14."""

from __future__ import annotations

import logging
import uuid

import pytest

from integration.conftest import (
    ASA,
    LOAD_MM,
    PETG,
    PLA,
    UUID_FIELD,
    World,
    charged,
    charges,
    count,
    events,
    logged,
    print_spec,
)
from integration.faults import set_extra_text
from integration.temperatures import TEMPERATURES
from simulator.model import State


async def baseline(w: World) -> None:
    records = await w.poll()
    assert len(events(records, "baseline")) == 1
    assert charges(records) == []


async def test_s1_spoolman_first_swap(world: World):
    """Spoolman updated before the unload: the swap commits there, the load is the new spool's."""
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    # Spoolman first: B joins A in the location. A stays active and is still charged.
    await w.move("B", w.tool_location)
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert len(events(records, "swap_staged")) == 1
    assert w.charged(records, "A") == 20

    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "unload")) == 1
    committed = events(records, "swap_committed")
    assert [e["message"] for e in committed] == [f"swap done: spool {a} -> {b}"]
    assert w.charged(records, "A") == 30
    assert await w.spoolman.location(w.spools["A"]) is None  # Evicted to no location.

    before = w.tool.extruded
    records = await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    records += await w.poll()
    assert w.tool.extruded - before == LOAD_MM
    assert w.charged(records, "B") == LOAD_MM
    assert w.charged(records, "A") == 0
    assert await w.used("B") > 0


async def test_s1_matching_tag_writes_nothing(world: World):
    """Spoolman first, and the new spool's tag already matches: nothing is written."""
    w = world
    b = w.spools["B"]
    await baseline(w)
    uuid_b = str(uuid.uuid4())
    await set_extra_text(w.spoolman.client, b, UUID_FIELD, uuid_b)
    await w.move("B", w.tool_location)
    await w.poll()
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_committed")) == 1

    records = await w.swap_cycle(
        poll_every=0.3, source="tag", name=PETG[0], color=PETG[1], uuid=uuid_b
    )
    records += await w.poll()
    w.advance(2.0)
    records += await w.poll()
    assert w.charged(records, "B") == LOAD_MM
    assert w.requests("PUT") == []
    assert w.material() == (PETG[0], PETG[1], uuid_b)


async def test_s1_tag_with_other_values_is_left_alone(world: World):
    """Spoolman first, and the tag differs in name, color and temperatures: not written (Q11).

    A write would end the printer's usage tracking on the tag. The tag's UUID is still
    adopted, since storing it writes only to Spoolman.
    """
    w = world
    b = w.spools["B"]
    await s1_swap_to_b(w)

    # A tag gives the midpoint of its ranges, here 250 for the nozzle and 80 for the bed.
    tag = str(uuid.uuid4())
    temperatures = (250, 170, 80)
    load = {"source": "tag", "name": ASA[0], "color": ASA[1], "uuid": tag}
    records = await w.swap_cycle(poll_every=0.3, temperatures=temperatures, **load)
    for _ in range(3):
        records += await w.poll()
        w.advance(2.0)
    assert w.charged(records, "B") == LOAD_MM
    assert w.requests("PUT") == []
    assert w.material() == (ASA[0], ASA[1], tag)
    assert w.printer.tools[0].material.temperatures() == temperatures
    assert w.printer.tools[0].material.material_uuid is not None  # Still linked to the tag.
    assert await w.spoolman.extra(b, UUID_FIELD) == tag
    assert await w.flag("B") is None


async def test_s1_menu_load_with_other_values_is_written(world: World):
    """Spoolman first, and a menu load of another type: Spoolman's spool is written."""
    w = world
    b = w.spools["B"]
    await s1_swap_to_b(w)
    await w.swap_cycle(poll_every=0.3, source="preset", name=ASA[0], color=ASA[1])
    await w.poll()
    puts = w.requests("PUT")
    assert len(puts) == 1 and " T220 B60 " in puts[0].data["body"]
    await w.run_until(lambda: w.printer.job is None)
    await w.poll()
    minted = await w.spoolman.extra(b, UUID_FIELD)
    assert minted is not None
    assert w.material() == (PETG[0], PETG[1], minted)
    assert w.printer.tools[0].material.temperatures() == TEMPERATURES


async def s1_swap_to_b(w: World) -> None:
    """S1 up to the load: B moved in, A unloaded, B active."""
    await baseline(w)
    await w.move("B", w.tool_location)
    await w.poll()
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_committed")) == 1


async def test_s1_tag_uuid_is_adopted(world: World):
    """The new spool has no UUID: it takes the tag's, and nothing is written (#66)."""
    w = world
    b = w.spools["B"]
    await s1_swap_to_b(w)
    tag = str(uuid.uuid4())
    load = {"source": "tag", "name": PETG[0], "color": PETG[1], "uuid": tag}
    records = await w.swap_cycle(poll_every=0.3, **load)
    records += await w.poll()
    w.advance(2.0)
    records += await w.poll()
    assert w.charged(records, "B") == LOAD_MM
    assert await w.spoolman.extra(b, UUID_FIELD) == tag
    assert w.requests("PUT") == []
    assert w.material() == (PETG[0], PETG[1], tag)
    assert await w.flag("B") is None

    # The next load of the same tag matches: still nothing written.
    await w.unload()
    await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1], uuid=tag)
    await w.poll()
    w.advance(2.0)
    await w.poll()
    assert w.requests("PUT") == []
    assert w.material() == (PETG[0], PETG[1], tag)


async def test_s1_untrusted_tag_is_left_alone(world: World):
    """A tag the bridge didn't see load isn't adopted, written or minted over (#66, Q11).

    The tag is loaded while the bridge restarts with no UUID seen saved, so it didn't
    watch the tag appear. Its temperatures differ from
    Spoolman's, which would make a write due for a menu load.
    """
    w = world
    b = w.spools["B"]
    await s1_swap_to_b(w)
    tag = str(uuid.uuid4())
    load = {"source": "tag", "name": PETG[0], "color": PETG[1], "uuid": tag}
    await w.swap_cycle(temperatures=(250, 170, 80), **load)
    for kept in w.bridge.state.printers.values():
        kept.seen_uuids.clear()
    w.bridge._save()
    await w.restart()
    for _ in range(3):
        await w.poll()
        w.advance(2.0)
    assert w.requests("PUT") == []
    assert w.material() == (PETG[0], PETG[1], tag)
    assert await w.spoolman.extra(b, UUID_FIELD) is None  # Not stored or minted.

    # A reload of the same tag with no Spoolman change is an unconfirmed swap: the tag
    # may be another spool's, so it still isn't stored, and still isn't written.
    await w.unload()
    await w.poll()
    await w.swap_cycle(poll_every=0.3, temperatures=(250, 170, 80), **load)
    for _ in range(3):
        await w.poll()
        w.advance(2.0)
    assert w.requests("PUT") == []
    assert w.material() == (PETG[0], PETG[1], tag)
    assert await w.spoolman.extra(b, UUID_FIELD) is None


async def test_s1_tag_uuid_conflict_is_flagged(world: World, caplog: pytest.LogCaptureFixture):
    """The new spool has a different UUID in Spoolman: flagged, neither overwritten (#66)."""
    w = world
    b = w.spools["B"]
    theirs = str(uuid.uuid4())
    await set_extra_text(w.spoolman.client, b, UUID_FIELD, theirs)
    await s1_swap_to_b(w)
    tag = str(uuid.uuid4())
    await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1], uuid=tag)
    for _ in range(3):
        await w.poll()
        w.advance(2.0)
    assert w.requests("PUT") == []
    assert w.material() == (PETG[0], PETG[1], tag)
    assert await w.spoolman.extra(b, UUID_FIELD) == theirs
    flag = await w.flag("B") or ""
    assert f"the loaded tag's instance UUID is {tag}, but this spool's is {theirs}" in flag
    assert count(caplog, logging.WARNING, f"the tag loaded has instance UUID {tag}") == 1

    # Clearing Spoolman's UUID settles it: the spool takes the tag's.
    await set_extra_text(w.spoolman.client, b, UUID_FIELD, "")
    await w.poll()
    assert await w.spoolman.extra(b, UUID_FIELD) == tag
    await w.poll()
    assert await w.flag("B") is None
    assert w.requests("PUT") == []


async def test_s2_filament_first_swap(world: World):
    """Filament swapped first: the charge is held, then moves once Spoolman is updated."""
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    assert w.charged(records, "A") == 30
    flag = await w.flag("A")
    assert flag is not None and "unloaded" in flag

    # Spoolman catches up before the next poll: A out, B in.
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    confirmed = events(records, "swap_confirmed")
    assert len(confirmed) == 1
    assert f"from spool {a} to {b}" in confirmed[0]["message"]
    assert await w.flag("A") is None

    records = await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    records += await w.poll()
    assert w.charged(records, "B") == LOAD_MM
    assert w.charged(records, "A") == 0


async def test_s14_several_swaps_between_polls(world: World):
    """Two swaps between polls: two unloads seen, the middle spool is charged nothing."""
    w = world
    await baseline(w)

    # A -> B -> C at the printer with the bridge not looking, then Spoolman says A -> C.
    w.printer.extrude(0, 30)
    await w.unload()
    await w.swap_cycle(source="tag", name=PETG[0], color=PETG[1])
    await w.unload()
    await w.swap_cycle(source="tag", name=ASA[0], color=ASA[1])
    await w.move("A", w.shelf)
    await w.move("C", w.tool_location)

    records = await w.poll()
    unloads = events(records, "unload")
    assert len(unloads) == 1
    assert unloads[0]["message"].startswith("2 unload(s)")
    committed = events(records, "swap_committed")
    assert [e["message"] for e in committed] == [
        f"swap done: spool {w.spools['A']} -> {w.spools['C']}"
    ]
    # Everything up to the second unload, B's load included, is A's.
    assert w.charged(records, "A") == 30 + LOAD_MM

    records += await w.poll()
    assert w.charged(records, "C") == LOAD_MM
    assert w.charged(records, "B") == 0
    assert {r["spool_id"] for r in charges(records)} == {w.spools["A"], w.spools["C"]}
    assert await w.used("B") == 0


async def stage_b(w: World, caplog: pytest.LogCaptureFixture) -> None:
    """Spoolman says B is in the tool with no unload: B is staged, A still charged."""
    a, b = w.spools["A"], w.spools["B"]
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    msg = f"spool {b} staged; still charging spool {a} until the printer reports an unload"
    assert [e["message"] for e in events(records, "swap_staged")] == [msg]
    assert logged(caplog, logging.INFO, f"[{w.tool_location}] {msg}")


async def test_s5_claim_after_printing_on_the_wrong_spool(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Print charged provisionally to the old spool: claiming the loaded one moves it there."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    assert w.charged(records, "A") == 30
    unloaded = w.mark()

    # B goes in at the printer from the menu, Spoolman is forgotten, and a print starts.
    await w.swap_cycle(source="preset", name=PETG[0])
    records = await w.poll()
    assert w.charged(records, "A") == LOAD_MM
    # A is still active and the printer shows B's type, so that poll wrote A back (W11).
    assert len(w.requests("PUT")) == 1
    await w.run_until(lambda: w.printer.job is None)
    records = await w.start_print(print_spec())
    msg = (
        "print started with no Spoolman change since the unload; assuming "
        f"spool {a} was reloaded and charging it provisionally"
    )
    assert [e["message"] for e in events(records, "swap_assumed")] == [msg]
    assert logged(caplog, logging.WARNING, f"[{w.tool_location}] {msg}")
    flag = await w.flag("A")
    assert flag is not None
    assert "charged to it provisionally" in flag and "tick 'Claim held usage'" in flag

    # The user moves B into the location, leaving A there too, and claims B.
    await w.move("B", w.tool_location)
    await w.claim("B")
    records = await w.poll()
    claimed = events(records, "claimed")
    assert len(claimed) == 1
    assert claimed[0]["message"].startswith(f"spool {b} claimed in place of spool {a}: moved ")
    assert claimed[0]["message"].endswith("(usage since the last unload)")
    since_unload = w.since(unloaded)
    assert charged(since_unload, a) == 0
    assert charged(since_unload, b) == w.counter - w.tool.extruded_at_unload
    assert logged(caplog, logging.INFO, f"[{w.tool_location}] moved spool {a} to")
    assert await w.spoolman.location(a) != w.tool_location
    assert logged(caplog, logging.INFO, f"cleared the claim on spool {b}")
    assert await w.spoolman.extra(b, "bridge_claim") is None
    assert await w.flag("A") is None
    puts = len(w.requests("PUT"))

    # The rest of the print is B's.
    at_claim = w.counter
    records = await w.finish_print()
    assert w.charged(records, "A") == 0
    assert w.charged(records, "B") == w.counter - at_claim

    # Idle with the printer showing A: the write of B queued at the claim runs.
    assert len(w.requests("PUT")) == puts + 1
    w.advance(1.5)
    minted = await w.spoolman.extra(b, "instance_uuid")
    assert minted is not None
    assert w.material() == (PETG[0], PETG[1], minted)


async def test_s13_staged_spool_claimed(world: World, caplog: pytest.LogCaptureFixture):
    """Staged spool, no unload: a claim moves the usage since the reboot and makes it active."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    await stage_b(w, caplog)

    w.printer.extrude(0, 30)
    records = await w.poll()
    assert w.charged(records, "A") == 30

    await w.claim("B")
    records = await w.poll()
    assert [e["message"] for e in events(records, "claimed")] == [
        f"spool {b} claimed in place of spool {a}: moved 30 mm from spool {a} "
        "(usage since the last reboot)"
    ]
    assert w.charged(records, "B") == 30
    assert w.charged(records, "A") == -30

    # B is active: the bridge writes it to the printer, and B is charged from here on.
    await w.run_until(lambda: w.printer.job is None)
    minted = await w.spoolman.extra(b, "instance_uuid")
    assert w.material() == (PETG[0], PETG[1], minted)
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert w.charged(records, "B") == 20
    assert w.charged(records, "A") == 0


async def test_s9_staged_spool_never_unloaded(world: World, caplog: pytest.LogCaptureFixture):
    """Staged and never unloaded: print starts and reboots flag it, and a claim makes it active."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    await stage_b(w, caplog)

    start = w.counter
    mark = w.mark()
    records = await w.start_print(print_spec())
    msg = f"print started while spool {b} is staged but not loaded; charging spool {a}"
    assert [e["message"] for e in events(records, "staged_at_print")] == [msg]
    assert logged(caplog, logging.WARNING, f"[{w.tool_location}] {msg}")
    flag_a, flag_b = await w.flag("A"), await w.flag("B")
    assert flag_a is not None and msg in flag_a
    assert flag_b is not None and msg in flag_b
    assert flag_b.endswith(" If this spool is the one loaded, tick 'Claim held usage' on it.")

    await w.run_until(
        lambda: w.printer.state is State.FINISHED and w.printer.job is None, poll_every=1.0
    )
    records = w.since(mark) + await w.poll()
    assert w.charged(records, "A") == w.counter - start
    assert w.charged(records, "B") == 0
    w.printer.dismiss()

    w.printer.reboot()
    records = await w.poll()
    assert len(events(records, "reboot")) == 1
    msg = (
        f"printer restarted while spool {b} is staged; an unload just before the restart "
        f"can be missed, so check which spool is loaded. Still charging spool {a}"
    )
    assert [e["message"] for e in events(records, "staged_at_reboot")] == [msg]
    assert logged(caplog, logging.WARNING, f"[{w.tool_location}] {msg}")
    flag_b = await w.flag("B")
    assert flag_b is not None and msg in flag_b

    await w.claim("B")
    records = await w.poll()
    # The reboot restarted the counters, so there's no usage to move.
    assert [e["message"] for e in events(records, "claimed")] == [
        f"spool {b} claimed in place of spool {a}: moved nothing (usage since the last reboot)"
    ]
    await w.run_until(lambda: w.printer.job is None)
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert w.charged(records, "B") == 20
    assert w.charged(records, "A") == 0


async def unconfirmed_swap(w: World) -> int:
    """Charge A 30 mm and unload with no Spoolman change; return the ledger mark after."""
    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    assert w.charged(records, "A") == 30
    return w.mark()


async def test_s3_spoolman_changed_during_the_load(world: World):
    """Filament first, old spool out mid-load, new one in mid-print: the new spool takes it all."""
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    unloaded = await unconfirmed_swap(w)
    at_unload = w.tool.extruded_at_unload

    # B goes in at the printer. The counted feed is A's until Spoolman says otherwise.
    w.printer.load(0, source="tag", name=PETG[0], color=PETG[1])
    records = await w.run_until(
        lambda: w.tool.process is not None and w.tool.process.phase == "PURGE", poll_every=0.3
    )
    records += await w.poll()
    on_a = w.charged(records, "A")
    assert on_a > 0

    # Mid-load, A is moved out: usage is held from here and the swap stays open.
    await w.move("A", w.shelf)
    records = await w.poll()
    assert [e["message"] for e in events(records, "active_unassigned")] == [
        f"spool {a} is no longer in the location; holding usage until a spool is assigned, "
        f"which also takes the {on_a:g} mm since the unload"
    ]
    records += await w.run_until(lambda: w.tool.process is None, poll_every=0.3)
    records += await w.poll()
    assert charges(records) == []
    flag = await w.flag("A")
    assert flag is not None and "this spool was moved out" in flag

    # Even a print start leaves the swap open, and its usage is held too.
    await w.run_until(lambda: w.printer.job is None)
    records = await w.start_print(print_spec())
    records += await w.poll()
    assert charges(records) == []
    assert events(records, "swap_assumed") == []

    # B moved in mid-print confirms the swap and takes everything since the unload.
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert [e["message"] for e in events(records, "swap_confirmed")] == [
        f"swap confirmed: {on_a:g} mm moved from spool {a} to {b}"
    ]
    records += await w.finish_print()
    since_unload = w.since(unloaded)
    assert charged(since_unload, a) == 0
    assert charged(since_unload, b) == w.counter - at_unload
    assert await w.flag("A") is None
    assert await w.spoolman.location(a) == w.shelf


async def test_s3_old_spool_moved_back(world: World):
    """Old spool moved out and back: the swap stays open, or closes as of a print start."""
    w = world
    a = w.spools["A"]
    await baseline(w)
    unloaded = await unconfirmed_swap(w)
    at_unload = w.tool.extruded_at_unload
    records = await w.swap_cycle(
        poll_every=0.3, source="tag", name=PLA[0], color=PLA[1], uuid=w.uuids["A"]
    )
    records += await w.poll()
    assert w.charged(records, "A") == LOAD_MM

    # Out and back between prints: as if A never left, the swap is still open.
    await w.move("A", w.shelf)
    await w.poll()
    w.printer.extrude(0, 10)
    records = await w.poll()
    assert charges(records) == []
    await w.move("A", w.tool_location)
    records = await w.poll()
    assert [e["message"] for e in events(records, "active_unassigned")] == [
        f"spool {a} is back in the location"
    ]
    assert w.charged(records, "A") == 10
    flag = await w.flag("A")
    assert flag is not None and "unloaded with no Spoolman change" in flag

    # Out again, and a print starts while the location is empty: held, swap still open.
    await w.move("A", w.shelf)
    await w.poll()
    await w.run_until(lambda: w.printer.job is None)
    records = await w.start_print(print_spec())
    assert charges(records) == [] and events(records, "swap_assumed") == []
    job = w.printer.job.id

    # A back mid-print: the swap closes as of that print start, charged provisionally.
    await w.move("A", w.tool_location)
    records = await w.poll()
    assert [e["message"] for e in events(records, "swap_assumed")] == [
        f"print {job} started with no Spoolman change since the unload; assuming "
        f"spool {a} was reloaded and charging it provisionally"
    ]
    await w.finish_print()
    assert charged(w.since(unloaded), a) == w.counter - at_unload
    flag = await w.flag("A")
    assert flag is not None
    assert f"{w.counter - at_unload} mm (≈" in flag and "charged to it provisionally" in flag


async def test_s3_unload_while_the_location_is_empty(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Unload while the swap's location is empty: closed on the old spool, held usage lost."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a = w.spools["A"]
    await baseline(w)
    unloaded = await unconfirmed_swap(w)
    await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    records = w.since(unloaded) + await w.poll()
    on_a = w.charged(records, "A")
    assert on_a == LOAD_MM
    # B was loaded from its tag, so A isn't written back over it (Q11).
    await w.run_until(lambda: w.printer.job is None)
    assert w.requests("PUT") == []
    assert w.material() == (PETG[0], PETG[1], None)

    await w.move("A", w.shelf)
    await w.poll()
    w.printer.extrude(0, 25)
    await w.unload()
    records = await w.poll()
    msg = "25 mm before unload had no spool"
    assert [e["message"] for e in events(records, "lost_unassigned")] == [msg]
    assert count(caplog, logging.WARNING, f"[{w.tool_location}] {msg}") == 1
    assert events(records, "swap_confirmed") == [] and charges(records) == []
    assert charged(w.since(unloaded), a) == LOAD_MM  # The swap's usage stays on A.
    assert await w.flag("A") is None

    # The next spool moved in takes only what came after the second unload.
    after = w.tool.extruded_at_unload
    await w.swap_cycle(poll_every=0.3, source="tag", name=ASA[0], color=ASA[1])
    await w.move("C", w.tool_location)
    records = await w.poll()
    assert w.charged(records, "C") == w.counter - after == LOAD_MM
    assert w.charged(records, "A") == 0


async def test_s3_moved_after_the_write_back(world: World):
    """Spoolman updated after the old spool was written back: confirmed, then B is written.

    B is loaded from the menu: a tag isn't written back over (Q11).
    """
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)
    unloaded = await unconfirmed_swap(w)
    await w.swap_cycle(poll_every=0.3, source="preset", name=PETG[0])
    await w.poll()
    await w.run_until(lambda: w.printer.job is None)
    assert len(w.requests("PUT")) == 1
    assert w.material() == (PLA[0], PLA[1], w.uuids["A"])
    # A poll sees the write-back took, so the rewrite below comes from the move, not the unload.
    await w.poll()
    assert not w.bridge.printers[0].tools[0].needs_sync

    # The move after the write-back still confirms the swap, and B is written over A.
    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    records = await w.poll()
    assert [e["message"] for e in events(records, "swap_confirmed")] == [
        f"swap confirmed: {LOAD_MM} mm moved from spool {a} to {b}"
    ]
    assert len(w.requests("PUT")) == 2  # Written in the poll that confirms the swap.
    await w.run_until(lambda: w.printer.job is None)
    name, color, uuid = w.material()
    assert (name, color) == PETG and uuid not in (None, w.uuids["A"])
    assert charged(w.since(unloaded), b) == LOAD_MM
    assert charged(w.since(unloaded), a) == 0


async def test_s4_same_spool_reloaded_then_print(world: World):
    """Same spool unloaded and reloaded, then a print: provisional charge, kept at the unload."""
    w = world
    a = w.spools["A"]
    await baseline(w)
    unloaded = await unconfirmed_swap(w)
    at_unload = w.tool.extruded_at_unload
    flag = await w.flag("A")
    assert flag is not None and "unloaded with no Spoolman change" in flag

    await w.swap_cycle(poll_every=0.3, source="tag", name=PLA[0], color=PLA[1], uuid=w.uuids["A"])
    await w.poll()
    await w.run_until(lambda: w.printer.job is None)
    records = await w.start_print(print_spec())
    assert [e["message"] for e in events(records, "swap_assumed")] == [
        "print started with no Spoolman change since the unload; assuming "
        f"spool {a} was reloaded and charging it provisionally"
    ]
    flag = await w.flag("A")
    assert flag is not None and "charged to it provisionally" in flag

    # Between prints the flag shows the whole amount since the unload.
    await w.finish_print()
    provisional = w.counter - at_unload
    flag = await w.flag("A")
    assert flag is not None and f" {provisional} mm (≈ " in flag

    w.printer.dismiss()
    await w.unload()
    records = await w.poll()
    assert [e["message"] for e in events(records, "provisional_kept")] == [
        f"unload keeps the {provisional} mm charged provisionally to spool {a}"
    ]
    since_unload = w.since(unloaded)
    assert all(c["spool_id"] == a and c["use_length"] > 0 for c in charges(since_unload))
    assert charged(since_unload, a) == provisional
    flag = await w.flag("A")
    assert flag is not None and "provisionally" not in flag  # Now the new unload's flag.
    assert len(events(records, "swap_unconfirmed")) == 1


async def test_s6_unload_with_nothing_assigned(world: World, caplog: pytest.LogCaptureFixture):
    """Unload with nothing assigned: the usage before it is lost, the usage after it held."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    await baseline(w)

    # A leaves Spoolman and the tool (S10), then B is loaded and unloaded unassigned.
    await w.move("A", w.shelf)
    await w.unload()
    records = await w.poll()
    assert len(events(records, "active_unassigned")) == 1
    await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    await w.poll()
    await w.unload()
    records = await w.poll()
    msg = f"{LOAD_MM} mm before unload had no spool"
    assert [e["message"] for e in events(records, "lost_unassigned")] == [msg]
    assert count(caplog, logging.WARNING, f"[{w.tool_location}] {msg}") == 1
    assert charges(records) == []

    # C's load is held until C is assigned, then charged to it.
    records = await w.swap_cycle(poll_every=0.3, source="tag", name=ASA[0], color=ASA[1])
    records += await w.poll()
    assert charges(records) == []
    await w.move("C", w.tool_location)
    records = await w.poll()
    c = w.spools["C"]
    assert [e["message"] for e in events(records, "assignment_changed")] == [f"spool {c} is active"]
    assert {r["spool_id"] for r in charges(records)} == {c}
    assert w.charged(records, "C") == LOAD_MM
    assert await w.used("B") == 0


async def test_s7_old_spool_out_new_in_then_unload(world: World):
    """Old spool out, new one in, then unload: the old one charged until the unload, new staged."""
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    await w.move("A", w.shelf)
    w.printer.extrude(0, 10)
    records = await w.poll()
    assert [e["message"] for e in events(records, "active_unassigned")] == [
        f"spool {a} is no longer in the location; still charging it until the printer "
        "reports an unload"
    ]
    assert w.charged(records, "A") == 10

    await w.move("B", w.tool_location)
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert [e["message"] for e in events(records, "swap_staged")] == [
        f"spool {b} staged; still charging spool {a} until the printer reports an unload"
    ]
    assert w.charged(records, "A") == 20

    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert [e["message"] for e in events(records, "swap_committed")] == [
        f"swap done: spool {a} -> {b}"
    ]
    assert w.charged(records, "A") == 30
    assert await w.spoolman.location(a) == w.shelf  # Already out: nothing to evict.

    records = await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    records += await w.poll()
    assert w.charged(records, "B") == LOAD_MM
    assert w.charged(records, "A") == 0


async def test_s8_staged_spool_moved_back_out(world: World, caplog: pytest.LogCaptureFixture):
    """Staged spool moved back out before an unload: staging cancelled, old spool charged."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    await w.move("B", w.tool_location)
    records = await w.poll()
    assert len(events(records, "swap_staged")) == 1

    await w.move("B", w.shelf)
    w.printer.extrude(0, 20)
    records = await w.poll()
    msg = f"spool {b} is no longer staged; still charging spool {a}"
    assert [e["message"] for e in events(records, "staging_cancelled")] == [msg]
    assert logged(caplog, logging.INFO, f"[{w.tool_location}] {msg}")
    assert w.charged(records, "A") == 20

    # B is forgotten: the next unload is an unconfirmed swap on A, not a swap to B.
    await w.unload()
    records = await w.poll()
    assert len(events(records, "swap_unconfirmed")) == 1
    assert events(records, "swap_committed") == []
    assert await w.used("B") == 0
    assert await w.spoolman.location(a) == w.tool_location


async def test_s10_spool_moved_out_with_no_replacement(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Spool moved out with no replacement: charged until the unload, then usage is held."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b = w.spools["A"], w.spools["B"]
    await baseline(w)

    await w.move("A", w.shelf)
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert len(events(records, "active_unassigned")) == 1
    assert w.charged(records, "A") == 20

    w.printer.extrude(0, 30)
    await w.unload()
    records = await w.poll()
    assert w.charged(records, "A") == 30
    assert [e["message"] for e in events(records, "active_unassigned")] == [
        f"spool {a} unloaded with no spool in the location; holding usage until one is assigned"
    ]
    assert events(records, "swap_unconfirmed") == []
    assert await w.flag("A") is None

    # The load is held, with one warning however many polls see it.
    records = await w.swap_cycle(poll_every=0.3, source="tag", name=PETG[0], color=PETG[1])
    records += await w.poll()
    assert charges(records) == []
    holding = f"[{w.tool_location}] holding usage: no spool assigned"
    assert count(caplog, logging.WARNING, holding) == 1

    await w.move("B", w.tool_location)
    records = await w.poll()
    assert [e["message"] for e in events(records, "assignment_changed")] == [f"spool {b} is active"]
    assert w.charged(records, "B") == LOAD_MM
    assert w.charged(records, "A") == 0


async def test_s11_two_spools_neither_active(world: World, caplog: pytest.LogCaptureFixture):
    """Two spools in the location, neither active: skipped and warned once until one leaves."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    a, b, c = w.spools["A"], w.spools["B"], w.spools["C"]
    await baseline(w)

    await w.move("A", w.shelf)
    await w.move("B", w.tool_location)
    await w.move("C", w.tool_location)
    records = []
    for _ in range(3):
        w.printer.extrude(0, 10)
        records += await w.poll()
    assert charges(records) == []
    ids = ", ".join(str(i) for i in sorted((b, c)))
    msg = f"[{w.tool_location}] location holds spools {ids}; skipping until it's clear"
    assert count(caplog, logging.WARNING, msg) == 1
    assert await w.spoolman.location(b) == await w.spoolman.location(c) == w.tool_location

    # C is removed: A, still loaded, takes the usage skipped meanwhile, and B is staged.
    await w.move("C", w.shelf)
    records = await w.poll()
    assert [e["message"] for e in events(records, "swap_staged")] == [
        f"spool {b} staged; still charging spool {a} until the printer reports an unload"
    ]
    assert w.charged(records, "A") == 30
    assert w.charged(records, "B") == w.charged(records, "C") == 0


async def test_s12_spoolman_corrected_to_the_loaded_uuid(world: World):
    """Spoolman changed to the spool whose UUID the printer reports: switched at once."""
    w = world
    a, b = w.spools["A"], w.spools["B"]
    uuid_b = "0b7d4c6e-5f2a-4b8e-9c3d-2e1f0a9b8c7d"

    # B went in before the bridge started, so its baseline wrongly takes A.
    await w.unload()
    await w.swap_cycle(source="tag", name=PETG[0], color=PETG[1], uuid=uuid_b)
    await baseline(w)
    flag = await w.flag("A")
    assert flag is not None and "printer filament differs" in flag

    # The user gives B its UUID and moves it into the location, with no unload.
    await set_extra_text(w.spoolman.client, b, UUID_FIELD, uuid_b)
    await w.move("B", w.tool_location)
    w.printer.extrude(0, 20)
    records = await w.poll()
    assert [e["message"] for e in events(records, "correction")] == [
        f"printer already has spool {b}'s filament; switching from spool {a} now"
    ]
    assert events(records, "swap_staged") == []
    assert w.charged(records, "B") == 20
    assert w.charged(records, "A") == 0
    assert await w.spoolman.location(a) is None  # Evicted, as for a swap.
    assert await w.flag("A") is None

    w.printer.extrude(0, 15)
    records = await w.poll()
    assert w.charged(records, "B") == 15
    assert w.requests("PUT") == []  # The printer already shows B.
