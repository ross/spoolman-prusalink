"""Two printers against one Spoolman: docs/scenarios.md rows X1 to X16.

Printer "it" plays the rows' printer A: location `<prefix> T` (the rows' `LA`), spool A
loaded. Printer "b" plays printer B: location `<prefix> LB`, spool E loaded. Both are
Core One Ls on one simulation, so `w.advance()` and `w.run_until()` move both.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import httpx
import pytest

from integration.conftest import (
    ASA,
    CLAIM_FIELD,
    PLA,
    World,
    baseline,
    charged,
    charges,
    count,
    event_fields,
    events,
    logged,
    logged_once,
    messages,
    print_spec,
)
from integration.printers import printer_of
from spoolman_prusalink.app import Bridge
from spoolman_prusalink.db import read_state

pytestmark = pytest.mark.parametrize("world_printers", [2], indirect=True)

IT, B = "it", "b"


def kinds(records: list[dict]) -> list[tuple[str, str]]:
    """Each event as (printer, kind), in ledger order."""
    return [(e["printer"], e["event"]) for e in events(records)]


async def settle(w: World, *names: str) -> None:
    """Tick until the printers named (default both) run no job, such as a sync job."""
    printers = [w.at(name).printer for name in names or (IT, B)]
    await w.run_until(lambda: all(p.job is None for p in printers))


async def load_a_at_b(w: World) -> list[dict]:
    """Unload E at b and load spool A there by its tag."""
    b = w.at(B)
    await b.unload()
    return await b.swap_cycle(source="tag", name=PLA[0], color=PLA[1], uuid=w.uuids["A"])


def bridge_for(w: World, cfg) -> Bridge:
    """A new bridge on `cfg`, replacing `w.bridge`.

    The caller closes the old bridge first; the world closes every bridge it started at
    the end.
    """
    return w.start_bridge(cfg)


@pytest.mark.parametrize("b_first", [False, True], ids=["a-first", "b-first"])
async def test_x1_spoolman_first_across_printers(world: World, b_first: bool):
    """Spool moved LA to LB first: A charges it until its unload, B stages it until its own."""
    w = world
    it, b = w.at(IT), w.at(B)
    a, e = w.spools["A"], w.spools["E"]
    await baseline(w)

    # S10 on it: A stays active with nothing assigned. S1 on b: A is staged over E.
    await w.move("A", b.location)
    it.extrude(50)  # 50 * 0.95 = 47.5: the counter reads 1047
    records = await w.poll()
    assert kinds(records) == [(IT, "active_unassigned"), (B, "swap_staged")]
    assert charged(records, a) == 47
    assert (it.state.active, it.state.assigned) == (a, None)
    assert (b.state.active, b.state.assigned) == (e, a)

    # Unload at it, and unload and load A at b, within one poll interval.
    async def at_it() -> None:
        it.extrude(10)
        await it.unload()

    async def at_b() -> None:
        b.extrude(30)
        await load_a_at_b(w)

    for step in (at_b, at_it) if b_first else (at_it, at_b):
        await step()
    records = await w.poll()
    assert kinds(records) == [
        (IT, "unload"),
        (IT, "unloaded_unassigned"),
        (B, "unload"),
        (B, "swap_committed"),
    ]
    assert it.state.active is None and b.state.active == a
    # 9.5 more, then the unload rams 23 * 0.95 = 21.85: 1078.85.
    assert charged(printer_of(records, IT), a) == 31
    # 30 * 0.95 = 28.5, then the unload's ram, 21.85: 2050.35.
    assert charged(printer_of(records, B), e) == 50
    # LOADS["8.1.0"], 78.85, on top of 2050.35: 2129.2.
    assert charged(printer_of(records, B), a) == 79
    assert await w.spoolman.location(e) is None  # b evicted E.
    assert await w.spoolman.location(a) == b.location

    # it holds usage with nothing assigned, as S10 says.
    await it.swap_cycle(source="preset", name=ASA[0])
    it.extrude(40)
    records = await w.poll()
    assert charges(records) == []
    assert it.state.active is None
    # The load, 78.85 on top of 1078.85: 1157.7. Its auto-retract left 55 mm retracted, so
    # the 40 mm extruded first feed 55 back into the 40 mm gap: (15 + 40) * 0.95 = 52.25,
    # 1209.95. Charged up to 1078.
    assert it.state.extruded - it.state.charged == 131


async def test_x2_filament_first_across_printers(world: World):
    """Unload at A, load at B, then move the spool: B's swap confirms, A's stays open and holds."""
    w = world
    it, b = w.at(IT), w.at(B)
    a, c, e = w.spools["A"], w.spools["C"], w.spools["E"]
    await baseline(w)

    # S2 on both: it unloads A; b unloads E and loads A, with no Spoolman change.
    it.extrude(30)  # 30 * 0.95 = 28.5
    await it.unload()
    b.extrude(20)  # 20 * 0.95 = 19
    await load_a_at_b(w)
    records = await w.poll()
    assert kinds(records) == [
        (IT, "unload"),
        (IT, "swap_unconfirmed"),
        (B, "unload"),
        (B, "swap_unconfirmed"),
    ]
    assert charged(records, a) == 50  # The unload rams 21.85 more: 1050.35.
    # b keeps charging its assigned spool: 19, the unload's ram 21.85 (2040.85), and
    # LOADS["8.1.0"], 78.85: 2119.7.
    assert charged(records, e) == 119
    unconfirmed = "filament was unloaded with no Spoolman change"
    assert f"{it.location}: {unconfirmed}" in (await w.flag("A") or "")
    assert f"{b.location}: {unconfirmed}" in (await w.flag("E") or "")
    await settle(w)  # W11: b writes E back while its swap is open.

    # Now Spoolman: E out of LB, A into it. LA is left empty.
    await w.move("E", w.shelf)
    await w.move("A", b.location)
    b.extrude(100)
    records = await w.poll()
    assert kinds(records) == [(IT, "swap_unassigned"), (B, "swap_confirmed")]
    assert b.state.active == a and b.state.swap is None
    # The usage since b's unload at 2040, the load, moves from E to A: 2119 - 2040 = 79.
    # The 100 mm first feed the load's 55 mm retraction back into the 40 mm gap:
    # (15 + 100) * 0.95 = 109.25, 2228.95. 2228 - 2040 = 188.
    assert charged(records, e) == -79
    assert charged(records, a) == 188
    assert await w.flag("E") is None
    # S3 on it: the swap stays open while LA is empty, and A carries that flag in b.
    assert it.state.active is None and it.state.swap is not None
    moved_out = f"{it.location}: filament was unloaded, then this spool was moved out"
    assert moved_out in (await w.flag("A") or "")
    await settle(w)

    # it loads ASA and prints a little: held, until C is moved into LA.
    await it.swap_cycle(source="preset", name=ASA[0])
    it.extrude(60)
    records = await w.poll()
    assert charges(records) == []
    assert moved_out in (await w.flag("A") or "")
    await w.move("C", it.location)
    records = await w.poll()
    assert kinds(records) == [(IT, "swap_confirmed")]
    assert it.state.active == c and it.state.swap is None
    # The load, 78.85 on top of 1050.35: 1129.2. The 60 mm first feed its 55 mm retraction
    # back: (15 + 60) * 0.95 = 71.25, 1200.45. Held since the unload at 1050: 150.
    assert charged(records, c) == 150
    assert await w.flag("A") is None


async def test_x3_spool_moved_but_never_loaded_elsewhere(world: World):
    """Spool moved LA to LB with no unload: A charges it, B stages it and flags it at a print."""
    w = world
    it, b = w.at(IT), w.at(B)
    a, e = w.spools["A"], w.spools["E"]
    await baseline(w)

    await w.move("A", b.location)
    it.extrude(50)  # 50 * 0.95 = 47.5
    records = await w.poll()
    assert kinds(records) == [(IT, "active_unassigned"), (B, "swap_staged")]
    assert charged(records, a) == 47  # S10 on it: still charged.
    assert (b.state.active, b.state.assigned) == (e, a)

    # S9 on b: a print starts with A staged. E is charged for it, and A is flagged.
    start = b.counter
    records = await b.start_print(print_spec())
    assert kinds(records) == [(B, "staged_at_print")]
    flag = await w.flag("A")
    assert flag is not None and b.location in flag and "staged" in flag
    records += await w.run_until(lambda: b.printer.job is None, poll_every=1.0)
    records += await w.poll()
    assert charged(records, e) == b.counter - start
    assert charged(records, a) == 0

    # S8 on b: moving A back cancels the staging there and resumes the assignment in it.
    await w.move("A", it.location)
    records = await w.poll()
    assert kinds(records) == [(IT, "active_reassigned"), (B, "staging_cancelled")]
    assert event_fields(records, "active_reassigned") == [{"spool": a}]
    assert (it.state.active, it.state.assigned) == (a, a)
    assert (b.state.active, b.state.assigned) == (e, e)
    assert await w.flag("A") is None


async def test_x4_evicted_spool_moves_to_the_other_printer(world: World):
    """A swap on A, the old spool moved into LB before the eviction: it's left there for B."""
    w = world
    it, b = w.at(IT), w.at(B)
    a, c, e = w.spools["A"], w.spools["C"], w.spools["E"]
    await baseline(w)

    # S1 on it: C staged, then unload and load. The user takes A to b before the poll.
    await w.move("C", it.location)
    assert kinds(await w.poll()) == [(IT, "swap_staged")]
    it.extrude(20)  # 20 * 0.95 = 19
    await it.unload()
    await it.swap_cycle(source="preset", name=ASA[0])
    await w.move("A", b.location)
    records = await w.poll()
    assert kinds(records) == [(IT, "unload"), (IT, "swap_committed"), (B, "swap_staged")]
    # 19, then the unload rams 21.85: 1040.85. LOADS["8.1.0"], 78.85, on top: 1119.7.
    assert charged(records, a) == 40 and charged(records, c) == 79
    # The eviction found A already out of LA, so it left it alone and cleared.
    assert await w.spoolman.location(a) == b.location
    assert it.state.evict == ()
    await settle(w)

    # S1 on b.
    b.extrude(15)  # 15 * 0.95 = 14.25
    await load_a_at_b(w)
    records = await w.poll()
    assert kinds(records) == [(B, "unload"), (B, "swap_committed")]
    assert b.state.active == a
    # 14.25, then the unload rams 21.85: 2036.1. LOADS["8.1.0"], 78.85, on top: 2114.95.
    assert charged(records, e) == 36 and charged(records, a) == 78
    assert await w.spoolman.location(a) == b.location
    assert await w.spoolman.location(e) is None
    assert await w.flag("A") is None


async def test_x4a_eviction_rereads_the_spool(world: World):
    """Spool moved into LB between the bridge's Spoolman read and its eviction: left in LB."""
    w = world
    it, b = w.at(IT), w.at(B)
    a, c = w.spools["A"], w.spools["C"]
    await baseline(w)

    await w.move("C", it.location)
    await w.poll()
    it.extrude(20)
    await it.unload()
    await it.swap_cycle(source="preset", name=ASA[0])

    # The eviction's re-read is the bridge's only read of the one spool: the user's move
    # lands just before it, after the poll's list read still showed A in LA.
    moved: list[str] = []

    async def race(request: httpx.Request) -> None:
        if request.method == "GET" and request.url.path == f"/api/v1/spool/{a}" and not moved:
            moved.append(request.url.path)
            await w.move("A", b.location)

    faults = w.spoolman_faults
    faults.hook = race
    faults.requests.clear()  # Only this poll's requests, for the order check below.
    records = await w.poll()
    assert kinds(records) == [(IT, "unload"), (IT, "swap_committed")]
    assert it.state.active == c and it.state.evict == (a,)
    assert moved == [f"/api/v1/spool/{a}"]
    sent = [r.rsplit(" ", 1)[0] for r in faults.requests]
    assert sent.index("GET /api/v1/spool") < sent.index(f"GET /api/v1/spool/{a}")
    assert f"PATCH /api/v1/spool/{a}" not in sent
    assert await w.spoolman.location(a) == b.location

    # The next poll sees A in LB, staged on b, and drops the eviction.
    records = await w.poll()
    assert kinds(records) == [(B, "swap_staged")]
    assert it.state.evict == ()
    assert await w.spoolman.location(a) == b.location


async def test_x5_claim_on_b_while_a_still_has_the_spool(
    world: World, caplog: pytest.LogCaptureFixture
):
    """Spool claimed in LB while A, off, still has it active: B takes it, A lets go on return."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    it, b = w.at(IT), w.at(B)
    a, e = w.spools["A"], w.spools["E"]
    await baseline(w)
    b.extrude(40)  # 40 * 0.95 = 38
    await w.poll()

    # it is off, so it never sees A leave. In Spoolman A goes into LB, E out, A claimed.
    it.printer.set_power("off")
    await w.move("E", w.shelf)
    await w.move("A", b.location)
    await w.claim("A")
    b.extrude(50)  # 50 * 0.95 = 47.5: the counter reads 2085
    records = await w.poll()
    assert kinds(records) == [(B, "claimed")]
    # S5 on b: what b charged E since its baseline moves to A, then b charges A.
    assert charged(records, e) == -38
    assert charged(records, a) == 38 + 47
    assert it.state.active == a and b.state.active == a  # Both, until it says otherwise.
    shared = f"spool {a} is active in {it.location} and {b.location} at once"
    assert logged_once(caplog, logging.WARNING, shared)
    assert "at once" in (await w.flag("A") or "")
    await w.poll()
    assert logged_once(caplog, logging.WARNING, shared)  # Logged once while it lasts.
    assert logged_once(caplog, logging.WARNING, "printer it unreachable")

    # A was taken out of it by hand while it was off. The boot counts that unload.
    it.printer.unload(0)
    it.printer.set_power("on")
    records = await w.poll()
    assert kinds(records) == [(IT, "reboot"), (IT, "unload"), (IT, "unloaded_unassigned")]
    unload = events(records, "unload")[0]
    assert (unload["mm"], unload["spool"]) == (0, a)
    assert it.state.active is None and b.state.active == a
    assert "at once" not in (await w.flag("A") or "")
    assert logged(caplog, logging.WARNING, "printer it reachable again")


async def test_x6_one_spool_active_in_two_tools(world: World, caplog: pytest.LogCaptureFixture):
    """A missed an unload over a reboot and B loads the spool: logged once, flagged, both charge."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    it, b = w.at(IT), w.at(B)
    a, c, e = w.spools["A"], w.spools["C"], w.spools["E"]
    await baseline(w)

    # Swapped by hand while it was off: A out, C's ASA in. The boot sees filament, so no
    # unload is counted, and the bridge sees only a reboot (Q2).
    it.printer.set_power("off")
    it.printer.unload(0)
    it.printer.load(0, source="preset", name=ASA[0])
    it.printer.set_power("on")
    records = await w.poll()
    assert kinds(records) == [(IT, "reboot")]
    assert it.state.active == a

    # Spoolman follows: C into LA, A into LB. Then b loads A (S1).
    await w.move("A", b.location)
    await w.move("C", it.location)
    records = await w.poll()
    assert kinds(records) == [(IT, "swap_staged"), (B, "swap_staged")]
    await settle(w)
    await load_a_at_b(w)
    records = await w.poll()
    assert kinds(records) == [(B, "unload"), (B, "swap_committed")]
    assert it.state.active == a and b.state.active == a
    shared = f"spool {a} is active in {it.location} and {b.location} at once"
    assert logged_once(caplog, logging.WARNING, shared)
    flag = f"active in {it.location} and {b.location} at once. One of them missed an unload"
    assert flag in (await w.flag("A") or "")
    assert await w.spoolman.location(e) is None
    await settle(w)

    # Both printers charge A. it rebooted with no gap: 10 * 0.95 = 9.5. b's unload rammed
    # 21.85 (2021.85) and its load counted 78.85 (2100.7); the 20 mm first feed the load's
    # 55 mm retraction back into the 40 mm gap: (15 + 20) * 0.95 = 33.25, 2133.95.
    it.extrude(10)
    b.extrude(20)
    records = await w.poll()
    assert charged(printer_of(records, IT), a) == 9
    assert charged(printer_of(records, B), a) == 33
    assert logged_once(caplog, logging.WARNING, shared)

    # A claim on the spool staged in it ends A's time there: what it charged A since the
    # reboot moves to C.
    await w.claim("C")
    records = await w.poll()
    assert kinds(records) == [(IT, "claimed")]
    assert it.state.active == c and b.state.active == a
    assert charged(records, a) == -9 and charged(records, c) == 9
    assert await w.flag("A") is None


async def test_x7_two_claims_one_per_printer(world: World):
    """Two spools claimed in one poll, one in LA and one in LB: each claim is its tool's."""
    w = world
    it, b = w.at(IT), w.at(B)
    a, b_spool, c, e = (w.spools[n] for n in "ABCE")
    await baseline(w)
    it.extrude(30)  # 30 * 0.95 = 28.5
    b.extrude(40)  # 40 * 0.95 = 38
    await w.poll()

    await w.move("C", it.location)
    await w.move("B", b.location)
    await w.claim("C")
    await w.claim("B")
    records = await w.poll()
    assert kinds(records) == [(IT, "claimed"), (B, "claimed")]
    assert (it.state.active, b.state.active) == (c, b_spool)
    assert charged(printer_of(records, IT), a) == -28 and charged(records, c) == 28
    assert charged(printer_of(records, B), e) == -38 and charged(records, b_spool) == 38
    assert await w.spoolman.location(a) is None and await w.spoolman.location(e) is None
    assert await w.spoolman.location(c) == it.location
    assert await w.spoolman.location(b_spool) == b.location


async def test_x8_one_printer_off(world: World, caplog: pytest.LogCaptureFixture):
    """Printer A off: logged once, B charged, A's eviction runs, A's claim waits for it."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    it, b = w.at(IT), w.at(B)
    a, e = w.spools["A"], w.spools["E"]
    cfg = w.bridge.config
    await baseline(w)

    # A swap on it whose eviction of A fails, so the eviction is still pending.
    await w.move("C", it.location)
    await w.poll()
    it.extrude(20)
    await it.unload()
    await it.swap_cycle(source="preset", name=ASA[0])
    faults = w.spoolman_faults
    faults.answer("PATCH", f"/api/v1/spool/{a}", 503, "busy", times=None, containing=b'"location"')
    records = await w.poll()
    assert kinds(records) == [(IT, "unload"), (IT, "swap_committed")]
    assert it.state.evict == (a,)
    assert await w.spoolman.location(a) == it.location
    await settle(w)
    faults.restore()

    # it goes off. Meanwhile B is moved into LA and claimed.
    it.printer.set_power("off")
    await w.move("B", it.location)
    await w.claim("B")
    b.extrude(70)  # 70 * 0.95 = 66.5
    records = await w.poll()
    assert kinds(records) == []
    assert charged(records, e) == 66
    assert await w.spoolman.location(a) is None  # The eviction pending in it ran.
    assert await w.spoolman.extra(w.spools["B"], CLAIM_FIELD) is True  # The claim waits.
    assert w.bridge.interval() == cfg.poll_idle
    await w.poll()
    assert logged_once(caplog, logging.WARNING, "printer it unreachable")

    # The interval follows b.
    b.printer.start_print(print_spec())
    w.advance(1.2)
    await w.poll()
    assert w.bridge.interval() == cfg.poll_printing
    await w.run_until(lambda: b.printer.job is None, poll_every=1.0)

    # Every printer off: the tick ends after the printer reads, with nothing from Spoolman.
    b.printer.set_power("off")
    faults.requests.clear()
    assert await w.poll() == []
    assert faults.requests == []

    # it comes back: the outage ends and the claim is handled.
    it.printer.set_power("on")
    b.printer.set_power("on")
    records = await w.poll()
    assert kinds(records) == [(IT, "reboot"), (IT, "claimed"), (B, "reboot")]
    assert it.state.active == w.spools["B"]
    assert logged(caplog, logging.WARNING, "printer it reachable again")
    assert logged_once(caplog, logging.WARNING, "printer it unreachable")


async def failing_eviction(w: World, caplog: pytest.LogCaptureFixture) -> str:
    """S1 on it, C for A, with Spoolman refusing to move A out; return the logged failure."""
    it = w.at(IT)
    a = w.spools["A"]
    await baseline(w)
    await w.move("C", it.location)
    await w.poll()
    it.extrude(20)
    await it.unload()
    await it.swap_cycle(source="preset", name=ASA[0])
    faults = w.spoolman_faults
    faults.answer("PATCH", f"/api/v1/spool/{a}", 503, "busy", times=None, containing=b'"location"')
    records = await w.poll()
    assert kinds(records) == [(IT, "unload"), (IT, "swap_committed")]
    await settle(w)
    for _ in range(2):
        await w.poll()
    assert it.state.evict == (a,)
    assert await w.spoolman.location(a) == it.location
    assert faults.requests.count(f"PATCH /api/v1/spool/{a} 503") == 3
    cant = f"[{it.location}] can't move spool {a} out: "
    assert logged_once(caplog, logging.WARNING, cant)
    assert count(caplog, logging.DEBUG, f"still can't move spool {a} out: ") == 2
    return cant


async def test_x4_failing_eviction_ends_when_the_spool_is_moved(
    world: World, caplog: pytest.LogCaptureFixture
):
    """An eviction Spoolman keeps refusing ends, said once, when the user moves the spool to B."""
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world
    it, b = w.at(IT), w.at(B)
    a = w.spools["A"]
    cant = await failing_eviction(w, caplog)

    # The user moves A into LB: it drops the eviction, and b stages A.
    await w.move("A", b.location)
    records = await w.poll()
    assert kinds(records) == [(B, "swap_staged")]
    assert it.state.evict == ()
    await w.poll()
    assert await w.spoolman.location(a) == b.location
    assert logged_once(caplog, logging.INFO, f"spool {a} no longer needs moving out")
    assert logged_once(caplog, logging.WARNING, cant)
    assert w.spoolman_faults.requests.count(f"PATCH /api/v1/spool/{a} 503") == 3


async def test_x8_failing_eviction_ends_while_the_printer_is_off(
    world: World, caplog: pytest.LogCaptureFixture
):
    """A's eviction keeps failing, A goes off, the user moves the spool out: ended once."""
    caplog.set_level(logging.DEBUG, logger="spoolman_prusalink")
    w = world
    it = w.at(IT)
    a = w.spools["A"]
    cant = await failing_eviction(w, caplog)

    # it is off, so its state still has the eviction, but A has left LA.
    it.printer.set_power("off")
    await w.move("A", w.shelf)
    for _ in range(2):
        await w.poll()
    assert it.state.evict == (a,)
    out = f"[{it.location}] spool {a} is out of the location now"
    assert logged_once(caplog, logging.INFO, out)
    assert w.spoolman_faults.requests.count(f"PATCH /api/v1/spool/{a} 503") == 3

    # it comes back and drops the eviction, with nothing more said about it.
    it.printer.set_power("on")
    for _ in range(2):
        await w.poll()
    assert it.state.evict == ()
    assert await w.spoolman.location(a) == w.shelf
    assert logged_once(caplog, logging.INFO, out)
    assert not logged(caplog, logging.INFO, f"spool {a} no longer needs moving out")
    assert logged_once(caplog, logging.WARNING, cant)


async def test_x9_spoolman_unreachable(world: World, caplog: pytest.LogCaptureFixture):
    """Spoolman down: printers still read; both printers' charges queue in one outbox, in order."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    it, b = w.at(IT), w.at(B)
    a, e = w.spools["A"], w.spools["E"]
    await baseline(w)

    # M1: no accounting. With the outbox empty the printers are still read, so b's
    # outage is logged on time.
    faults = w.spoolman_faults
    faults.cut()
    it.extrude(10)
    b.printer.set_power("off")
    assert await w.poll() == []
    assert logged_once(caplog, logging.WARNING, "Spoolman unreachable")
    assert logged_once(caplog, logging.WARNING, "printer b unreachable")
    b.printer.set_power("on")
    b.extrude(20)

    # Spoolman answers but refuses charges: both printers' charges queue, it before b.
    faults.restore()
    faults.answer("PUT", f"/api/v1/spool/{a}/use", 503, "busy", times=None)
    faults.answer("PUT", f"/api/v1/spool/{e}/use", 503, "busy", times=None)
    records = await w.poll()
    assert kinds(records) == [(B, "reboot")]
    assert charges(records) == []
    queued = [(r.printer, r.spool_id, r.use_length) for r in w.bridge.state.outbox]
    # it's 10 * 0.95 = 9.5; b rebooted, then 20 * 0.95 = 19.
    assert queued == [(IT, a, 9), (B, e, 19)]

    # Down again with charges queued: the tick stops at the failed flush, before any read.
    faults.cut()
    mark = len(w.sim.events)
    assert await w.poll() == []
    assert [ev for ev in w.sim.events[mark:] if ev.kind == "request"] == []

    # Back: the queue is sent in order.
    faults.restore()
    faults.requests.clear()
    records = await w.poll()
    assert [(r["printer"], r["spool_id"], r["use_length"]) for r in charges(records)] == queued
    assert faults.requests[:2] == [
        f"PUT /api/v1/spool/{a}/use 200",
        f"PUT /api/v1/spool/{e}/use 200",
    ]
    assert len(w.bridge.state.outbox) == 0
    assert logged(caplog, logging.WARNING, "sent 2 queued charge(s)")


async def test_x10_firmware_without_the_filament_endpoint(
    world: World, caplog: pytest.LogCaptureFixture
):
    """A's firmware lacks the filament endpoint: logged once, nothing charged, B unaffected."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    it, b = w.at(IT), w.at(B)
    a, e = w.spools["A"], w.spools["E"]
    cfg = w.bridge.config
    it.printer.set_firmware("no_endpoint")
    records = await w.poll()
    assert kinds(records) == [(B, "baseline")]

    it.extrude(30)
    b.extrude(100)
    records = await w.poll()
    await w.poll()
    assert charged(records, e) == 95 and charges(records, a) == []  # 100 * 0.95
    unsupported = "printer it: firmware has no /api/v1/filament"
    assert logged_once(caplog, logging.ERROR, unsupported)
    assert logged(caplog, logging.ERROR, "this firmware isn't supported")
    assert w.bridge.interval() == cfg.poll_idle

    # The status of it still reads, so a print there sets the printing rate.
    it.printer.start_print(print_spec())
    w.advance(1.2)
    assert charges(await w.poll()) == []
    assert w.bridge.interval() == cfg.poll_printing
    await w.run_until(lambda: it.printer.job is None, poll_every=1.0)
    await w.poll()
    assert w.bridge.interval() == cfg.poll_idle

    # Firmware that reports filament: it starts from a baseline, as on a first start.
    it.printer.set_firmware("full")
    records = await w.poll()
    assert kinds(records) == [(IT, "baseline")]
    assert logged(caplog, logging.ERROR, "printer it now reports filament; resuming")
    # 28.5, the 400 mm print's 380 and its end's ram, 21.85: 1430.35. The 5 mm first feed
    # the end's 55 mm retraction back into the 40 mm gap: (15 + 5) * 0.95 = 19, 1449.35.
    it.extrude(5)
    assert charged(await w.poll(), a) == 19
    assert logged_once(caplog, logging.ERROR, unsupported)


async def test_x11_charge_rejected_with_404(world: World, caplog: pytest.LogCaptureFixture):
    """A 404 on a charge is held for the record's printer and tool, not the other printer's."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    it, b = w.at(IT), w.at(B)
    a, c, e = w.spools["A"], w.spools["C"], w.spools["E"]
    await baseline(w)

    # A is active in it and staged in b (X3).
    await w.move("A", b.location)
    records = await w.poll()
    assert kinds(records) == [(IT, "active_unassigned"), (B, "swap_staged")]

    # A is deleted after the poll's list read, just before the charge to it from printer it.
    async def delete(request: httpx.Request) -> None:
        if request.method == "PUT" and request.url.path == f"/api/v1/spool/{a}/use":
            resp = await w.spoolman.client.delete(f"/api/v1/spool/{a}")
            resp.raise_for_status()

    faults = w.spoolman_faults
    faults.hook = delete
    it.extrude(25)  # 25 * 0.95 = 23.75
    records = await w.poll()
    faults.hook = None
    failed = [r for r in records if "failed" in r]
    assert [(r["printer"], r["spool_id"], r["failed"], r.get("held")) for r in failed] == [
        (IT, a, "404", True)
    ]
    assert (it.state.active, it.state.deleted_spool, it.state.held_mm) == (None, a, 23)
    assert (b.state.active, b.state.deleted_spool, b.state.held_mm) == (e, None, 0)
    assert logged(caplog, logging.WARNING, f"spool {a} was deleted in Spoolman")

    # it keeps holding. b's staging ends with A gone, and b charges E as before.
    it.extrude(15)  # 14.25 more: 1038
    records = await w.poll()
    assert kinds(records) == [(B, "staging_cancelled")]
    assert charges(records) == []
    assert it.state.held_mm == 38

    # The next spool active in it takes what it held. b gets none of it.
    await w.move("C", it.location)
    records = await w.poll()
    assert kinds(records) == [(IT, "assignment_changed"), (IT, "held_charged")]
    assert charged(records, c) == 38 and charges(records, e) == []
    b.extrude(5)  # 5 * 0.95 = 4.75
    assert charged(await w.poll(), e) == 4

    # The same from printer b: E deleted just before b's charge is held for b's tool, found
    # by the record's printer, and it, first in the config, holds none of it.
    async def delete_e(request: httpx.Request) -> None:
        if request.method == "PUT" and request.url.path == f"/api/v1/spool/{e}/use":
            resp = await w.spoolman.client.delete(f"/api/v1/spool/{e}")
            resp.raise_for_status()

    faults.hook = delete_e
    b.extrude(10)  # 9.5 more: 2014.25
    records = await w.poll()
    faults.hook = None
    failed = [r for r in records if "failed" in r]
    assert [(r["printer"], r["spool_id"], r["failed"], r.get("held")) for r in failed] == [
        (B, e, "404", True)
    ]
    assert (b.state.active, b.state.deleted_spool, b.state.held_mm) == (None, e, 10)
    assert (it.state.active, it.state.deleted_spool, it.state.held_mm) == (c, None, 0)


async def test_x14_printer_renamed(world: World, caplog: pytest.LogCaptureFixture):
    """A printer renamed: its old state kept but inert, with a warning; the new name baselines."""
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    w = world
    it = w.at(IT)
    a, c = w.spools["A"], w.spools["C"]
    await baseline(w)

    # Leave it with an eviction pending (A) and a charge queued (C's load).
    await w.move("C", it.location)
    await w.poll()
    it.extrude(20)
    await it.unload()
    await it.swap_cycle(source="preset", name=ASA[0])
    faults = w.spoolman_faults
    faults.answer("PATCH", f"/api/v1/spool/{a}", 503, "busy", times=None, containing=b'"location"')
    faults.answer("PUT", f"/api/v1/spool/{c}/use", 503, "busy", times=None)
    records = await w.poll()
    assert kinds(records) == [(IT, "unload"), (IT, "swap_committed")]
    assert it.state.evict == (a,)
    assert [(r.printer, r.spool_id) for r in w.bridge.state.outbox] == [(IT, c)]
    await settle(w)
    await w.bridge.aclose()

    # C is deleted, and it is renamed in the config.
    resp = await w.spoolman.client.delete(f"/api/v1/spool/{c}")
    resp.raise_for_status()
    renamed = replace(w.cfg, printers=(replace(w.cfg.printers[0], name="it2"), w.cfg.printers[1]))
    warning = "the database has a printer 'it' that isn't configured; keeping its state"
    bridge_for(w, renamed)
    assert logged_once(caplog, logging.WARNING, warning)
    records = await w.poll()
    await w.poll()
    assert kinds(records) == [("it2", "baseline")]
    # The 404 isn't held for the old name's tool: it's dropped.
    failed = [r for r in records if "failed" in r]
    assert [(r["printer"], r["spool_id"], r["failed"], r.get("held")) for r in failed] == [
        (IT, c, "404", None)
    ]
    # 19, the unload's ram 21.85 (1040.85), then LOADS["8.1.0"], 78.85: 1119.7.
    assert logged(caplog, logging.ERROR, f"Spoolman rejected a +79 mm charge to spool {c}")
    # The old name's eviction never runs, and its state stays in the database.
    assert await w.spoolman.location(a) == it.location
    saved = read_state(w.cfg.db_file).printers
    assert saved[IT].tools[0].evict == (a,)
    assert set(saved) == {IT, "it2", B}
    await w.bridge.aclose()

    bridge_for(w, renamed)  # Warned at each start.
    assert len(messages(caplog, None, warning)) == 2  # At any level.
    assert count(caplog, logging.WARNING, warning) == 2


async def test_x16_runout_flag_clears_when_active_elsewhere(world: World):
    """A runout flag set on A clears when the spool becomes active in B after another spool."""
    w = world
    it, b = w.at(IT), w.at(B)
    a = w.spools["A"]
    await baseline(w)

    # M5a on it: A runs out mid-print with nearly all of it left in Spoolman.
    await it.start_print(print_spec())
    it.printer.runout(0)
    records = await w.poll()
    assert kinds(records) == [(IT, "unload"), (IT, "swap_unconfirmed")]
    runout = f"{it.location}: unloaded mid-print with"
    assert runout in (await w.flag("A") or "")
    assert a in w.bridge.state.runouts

    # A goes to b, where E was active: staged, then loaded (S1).
    await w.move("E", w.shelf)
    await w.move("A", b.location)
    records = await w.poll()
    assert kinds(records) == [(IT, "swap_unassigned"), (B, "swap_staged")]
    assert runout in (await w.flag("A") or "")
    await settle(w, B)
    await load_a_at_b(w)
    records = await w.poll()
    assert kinds(records) == [(B, "unload"), (B, "swap_committed")]
    assert b.state.active == a
    assert w.bridge.state.runouts == {}
    flag = await w.flag("A") or ""
    assert runout not in flag
    # The swap in it is still open with A moved out, so that part of the flag stays.
    assert f"{it.location}: filament was unloaded, then this spool was moved out" in flag
