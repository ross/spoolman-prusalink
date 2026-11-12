"""Two printers against one Spoolman: the X scenarios in docs/scenarios.md."""

import httpx
from harness import (
    CLAIMED,
    PLA,
    drop,
    fill,
    jobs,
    ledger,
    menu,
    restart,
    spool,
    start_job,
    two_printers,
)

from spoolman_prusalink.accounting import ToolState
from spoolman_prusalink.db import read_state
from spoolman_prusalink.state import Runout

A, B = "Tool a", "Tool b"


async def baseline(tmp_path, spools, **cfg):
    """A bridge with spool 19 loaded in a and spool 20 in b, both charged once."""
    bridge, fake, printers = two_printers(tmp_path, spools, **cfg)
    await bridge.poll()  # Baselines.
    for p in printers.values():
        p.tool.extruded += 100
    await bridge.poll()
    return bridge, fake, printers


def tools(bridge):
    return {p.name: p.tools[0] for p in bridge.printers}


def unload(station, *, load=None):
    station.tool.unload_count += 1
    station.tool.extruded_at_unload = station.tool.extruded
    fill(station.tool, load)


async def test_x1_spoolman_first_then_unload_at_a_and_load_at_b(tmp_path):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    assert fake.used == {19: [100.0], 20: [100.0]}

    # Spoolman first: 19 moves to b's location while still loaded in a.
    fake.spools[19]["location"] = B
    a.tool.extruded += 50
    await bridge.poll()
    st = tools(bridge)
    assert st["a"].active == 19 and st["a"].assigned is None  # S10 on a.
    assert (st["b"].active, st["b"].assigned) == (20, 19)  # S1 on b: staged.
    assert fake.used[19] == [100.0, 50.0]

    # Unload at a and load into b, within one poll interval, in either order.
    a.tool.extruded += 10
    unload(a)
    b.tool.extruded += 30  # b's last usage on 20.
    unload(b, load=PLA)
    b.tool.extruded += 300  # The load on 19.
    await bridge.poll()
    st = tools(bridge)
    assert st["a"].active is None and st["b"].active == 19
    assert fake.used == {19: [100.0, 50.0, 10.0, 300.0], 20: [100.0, 30.0]}
    assert ("location", 20, None) in fake.writes  # b evicted 20.
    assert fake.spools[19]["location"] == B

    # a holds usage with nothing assigned, as S10 says.
    fill(a.tool, menu("PLA"))
    a.tool.extruded += 40
    await bridge.poll()
    assert fake.used[19] == [100.0, 50.0, 10.0, 300.0]
    held = tools(bridge)["a"]
    assert held.active is None and held.extruded - held.charged == 40
    await bridge.aclose()


async def test_x2_filament_first_across_printers(tmp_path):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]

    # Unload at a, unload at b and load a's spool there. No Spoolman change yet.
    unload(a)
    unload(b, load=PLA)
    b.tool.extruded += 200
    await bridge.poll()
    st = tools(bridge)
    assert st["a"].swap is not None and st["a"].swap.spool_id == 19  # S2 on a.
    assert st["b"].swap is not None and st["b"].swap.spool_id == 20  # S2 on b.
    assert fake.used[20] == [100.0, 200.0]  # b keeps charging its assigned spool.
    assert "Tool a: filament was unloaded with no Spoolman change" in fake.flag(19)
    assert "Tool b: filament was unloaded with no Spoolman change" in fake.flag(20)

    # Now Spoolman: 20 out of b's location, 19 into it. a's location is empty.
    fake.spools[20]["location"] = None
    fake.spools[19]["location"] = B
    b.tool.extruded += 100
    await bridge.poll()
    st = tools(bridge)
    assert st["b"].active == 19 and st["b"].swap is None  # Confirmed on b.
    assert fake.used[20] == [100.0, 200.0, -200.0] and fake.used[19] == [100.0, 200.0, 100.0]
    assert st["a"].active is None and st["a"].swap is not None  # S3 on a: open, held.
    assert fake.flag(20) is None
    assert "Tool a: filament was unloaded, then this spool was moved out" in fake.flag(19)

    # Whatever went into a confirms a's swap and takes the usage held there.
    fill(a.tool, menu("PLA"))
    a.tool.extruded += 60
    await bridge.poll()
    fake.spools[22] = spool(22, A)
    await bridge.poll()
    st = tools(bridge)
    assert st["a"].active == 22 and st["a"].swap is None
    assert fake.used[22] == [60.0]
    assert fake.flag(19) is None
    await bridge.aclose()


async def test_x3_spool_moved_in_spoolman_but_never_loaded_elsewhere(tmp_path):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    fake.spools[19]["location"] = B
    a.tool.extruded += 50
    await bridge.poll()
    st = tools(bridge)
    assert st["a"].active == 19 and fake.used[19] == [100.0, 50.0]  # S10: still charged.
    assert st["b"].assigned == 19 and st["b"].active == 20  # S9: staged on b.
    start_job(b)
    await bridge.poll()
    b.tool.extruded += 10
    await bridge.poll()  # The print starts at its first extrusion (Q4).
    assert "staged" in (fake.flag(19) or "") and "Tool b" in fake.flag(19)
    # Moving it back cancels the staging on b and resumes a's assignment.
    fake.spools[19]["location"] = A
    await bridge.poll()
    st = tools(bridge)
    assert st["b"].assigned == 20 and st["a"].assigned == 19
    assert "staging_cancelled" in {r.get("event") for r in ledger(tmp_path)}
    await bridge.aclose()


async def test_x4_evicted_spool_moves_to_the_other_printer(tmp_path):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    fake.spools[22] = spool(22, A)  # Spoolman first on a: 19 -> 22.
    await bridge.poll()
    unload(a, load=PLA)
    a.tool.extruded += 100
    await bridge.poll()
    assert tools(bridge)["a"].active == 22
    assert ("location", 19, None) in fake.writes and fake.spools[19]["location"] is None
    # The user moves 19 into b's location and loads it there.
    fake.spools[19]["location"] = B
    unload(b, load=PLA)
    b.tool.extruded += 100
    await bridge.poll()
    st = tools(bridge)
    assert st["b"].active == 19 and fake.spools[20]["location"] is None
    assert fake.used[19] == [100.0, 100.0]  # a's last usage and b's load.
    await bridge.aclose()


async def test_x4a_eviction_rereads_the_spool_before_moving_it(tmp_path):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a = printers["a"]
    fake.spools[22] = spool(22, A)
    await bridge.poll()
    unload(a, load=PLA)
    a.tool.extruded += 100

    # Between the bridge's spool read and its eviction, the user moves 19 into b's
    # location: the single-spool read sees the new location.
    handle = fake.handle

    def racing(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/spool/19":
            fake.spools[19]["location"] = B
        return handle(request)

    fake.handle = racing
    await bridge.poll()
    assert tools(bridge)["a"].active == 22
    assert fake.spools[19]["location"] == B
    assert not [w for w in fake.writes if w[0] == "location"]
    await bridge.poll()  # The next accounting pass drops the eviction.
    assert tools(bridge)["a"].evict == ()
    await bridge.aclose()


async def test_x5_x6_claim_on_b_while_a_still_has_the_spool(tmp_path, caplog):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    caplog.set_level("INFO")
    a.before_request = drop  # a is off, so it never reports the unload.
    fake.spools[20]["location"] = None
    fake.spools[19]["location"] = B
    fake.spools[19]["extra"].update(CLAIMED)
    b.tool.extruded += 50
    await bridge.poll()
    st = tools(bridge)
    assert st["b"].active == 19 and st["a"].active == 19  # Both, until a says otherwise.
    # The claim moves what b charged since its last load: the 100 on 20.
    assert fake.used[19] == [100.0, 100.0, 50.0] and fake.used[20] == [100.0, -100.0]
    warning = "spool 19 is active in Tool a and Tool b at once"
    assert caplog.text.count(warning) == 1
    assert "active in Tool a and Tool b at once" in fake.flag(19)
    await bridge.poll()
    assert caplog.text.count(warning) == 1  # Logged once while it lasts.

    # a comes back and sees its unload: usage before it was a's, so it goes to 19.
    a.before_request = None
    a.tool.extruded += 10
    unload(a)
    await bridge.poll()
    st = tools(bridge)
    assert st["a"].active is None and st["b"].active == 19
    assert fake.used[19] == [100.0, 100.0, 50.0, 10.0]
    assert "at once" not in (fake.flag(19) or "")
    recovered = [
        r for r in caplog.records if "no longer active in more than one tool" in r.getMessage()
    ]
    assert [r.levelname for r in recovered] == ["WARNING"]
    await bridge.aclose()


async def test_x6_shared_spool_logs_again_when_it_recurs(tmp_path, caplog):
    # When both tools drop the spool in one poll, it never passes through being active in
    # exactly one tool, and the "shared" problem must still clear so a recurrence logs.
    bridge, fake, printers = two_printers(tmp_path, [spool(19, A), spool(20, B)])
    a_tools, b_tools = bridge.state.tools("a"), bridge.state.tools("b")
    warning = "spool 19 is active in Tool a and Tool b at once"
    for _ in range(2):
        a_tools[0] = ToolState(boot_id=1, active=19, assigned=19)
        b_tools[0] = ToolState(boot_id=1, active=19, assigned=20)
        bridge._check_shared({})
        bridge._check_shared({})
        a_tools[0] = ToolState(boot_id=1, active=None, assigned=19)
        b_tools[0] = ToolState(boot_id=1, active=None, assigned=20)
        bridge._check_shared({})
    assert caplog.text.count(warning) == 2
    assert caplog.text.count("spool 19 is no longer active in more than one tool") == 2
    await bridge.aclose()


async def test_shared_spool_logs_again_when_its_locations_change(tmp_path, caplog):
    bridge, fake, printers = two_printers(tmp_path, [spool(19, A), spool(20, B)])
    a_tools, b_tools = bridge.state.tools("a"), bridge.state.tools("b")
    a_tools[0] = ToolState(boot_id=1, active=19, assigned=19)
    b_tools[0] = ToolState(boot_id=1, active=19, assigned=20)
    bridge._check_shared({})
    bridge._check_shared({})
    assert caplog.text.count("spool 19 is active in Tool a and Tool b at once") == 1
    bridge.printers[0].config.tools[0] = "Tool c"
    bridge._check_shared({})
    bridge._check_shared({})
    assert caplog.text.count("spool 19 is active in Tool c and Tool b at once") == 1
    assert "no longer active" not in caplog.text
    await bridge.aclose()


async def test_archived_spool_on_the_first_printer_is_looked_up_once(tmp_path, caplog):
    # The archived-spool cache is shared: b's check must not drop what a found.
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a = printers["a"]
    fake.spools[19]["archived"] = True
    for _ in range(3):
        a.tool.extruded += 100
        await bridge.poll()
    assert fake.lookups == [19]
    assert fake.flag(19) == "Tool a: spool is archived but still loaded"
    assert caplog.text.count("spool 19 is archived but still loaded") == 1
    await bridge.aclose()


async def test_eviction_reread_with_a_malformed_body_is_logged_and_retried(tmp_path, caplog):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a = printers["a"]
    fake.spools[22] = spool(22, A)
    await bridge.poll()
    unload(a, load=PLA)
    a.tool.extruded += 100
    handle = fake.handle

    def bad_gateway(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/spool/19":
            return httpx.Response(200, text="<html>bad gateway</html>")
        return handle(request)

    fake.handle = bad_gateway
    await bridge.poll()  # Doesn't raise; the eviction waits.
    await bridge.poll()
    assert caplog.text.count("can't move spool 19 out") == 1
    assert fake.spools[19]["location"] == A and tools(bridge)["a"].active == 22
    fake.handle = handle
    await bridge.poll()
    assert fake.spools[19]["location"] is None
    await bridge.aclose()


async def test_x7_two_claims_one_per_printer(tmp_path):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    fake.spools[23] = {**spool(23, A), "extra": dict(CLAIMED)}
    fake.spools[24] = {**spool(24, B), "extra": dict(CLAIMED)}
    await bridge.poll()
    st = tools(bridge)
    assert (st["a"].active, st["b"].active) == (23, 24)
    assert fake.used == {19: [100.0, -100.0], 20: [100.0, -100.0], 23: [100.0], 24: [100.0]}
    assert fake.spools[19]["location"] is None and fake.spools[20]["location"] is None
    await bridge.aclose()


async def test_x8_one_printer_off(tmp_path, caplog):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    caplog.set_level("INFO")
    a.before_request = drop
    # An eviction pending on a's tool doesn't need a.
    a_tools = bridge.state.tools("a")
    a_tools[0] = ToolState(**{**a_tools[0].__dict__, "evict": (25,)})
    fake.spools[25] = spool(25, A)
    b.tool.extruded += 70
    await bridge.poll()
    assert caplog.text.count("printer a unreachable") == 1
    assert fake.used[20] == [100.0, 70.0]
    assert fake.spools[25]["location"] is None
    assert bridge.interval() == bridge.config.poll_idle
    start_job(b)
    await bridge.poll()
    assert bridge.interval() == bridge.config.poll_printing
    a.before_request = None
    await bridge.poll()
    back = [r.levelname for r in caplog.records if "printer a reachable again" in r.getMessage()]
    assert back == ["WARNING"]
    await bridge.aclose()


async def test_x8_an_unread_printers_flags_stay_set(tmp_path, caplog):
    # a has an unconfirmed swap flagged when it goes off. The flag stays in Spoolman
    # while a is off and when it comes back, including across a restart while a is off.
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    unload(a)
    await bridge.poll()
    text = "unloaded with no Spoolman change"
    assert text in fake.flag(19)
    a.before_request = drop
    b.tool.extruded += 10
    await bridge.poll()
    assert text in fake.flag(19)
    restarted = restart(bridge)
    await restarted.poll()
    assert text in fake.flag(19)
    a.before_request = None
    await restarted.poll()
    assert text in fake.flag(19)
    assert jobs(a) == []  # A flag runs no job.
    await restarted.aclose()


async def test_x9_spoolman_down_queues_both_printers_charges_in_order(tmp_path):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    fake.down = True
    a.tool.extruded += 10
    b.tool.extruded += 20
    await bridge.poll()
    assert fake.used == {19: [100.0], 20: [100.0]}
    fake.down = False
    fake.fail_use = [503]  # The first retry fails, so the queue is saved with both.
    await bridge.poll()
    queued = read_state(tmp_path / "bridge.db").outbox
    assert [(r["printer"], r["spool_id"]) for r in queued] == [("a", 19), ("b", 20)]
    await bridge.poll()
    assert fake.used == {19: [100.0, 10.0], 20: [100.0, 20.0]}
    await bridge.aclose()


async def test_x10_unsupported_firmware_on_one_printer(tmp_path, caplog):
    bridge, fake, printers = two_printers(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    a.printer.set_firmware("no_endpoint")
    caplog.set_level("INFO")
    await bridge.poll()
    b.tool.extruded += 100
    await bridge.poll()
    assert caplog.text.count("printer a: ") == 1 and "isn't supported" in caplog.text
    assert fake.used == {20: [100.0]}
    # a's status still reads, so a doesn't hold the bridge at the printing rate.
    assert bridge.interval() == bridge.config.poll_idle
    a.printer.set_firmware("full")
    await bridge.poll()
    resumed = [r.levelname for r in caplog.records if "a now reports filament" in r.getMessage()]
    assert resumed == ["ERROR"]
    await bridge.aclose()


async def test_read_failures_name_the_printer(tmp_path, caplog):
    bridge, fake, printers = two_printers(tmp_path, [spool(19, A), spool(20, B)])
    printers["a"].printer.set_fault("bad_key")
    await bridge.poll()
    assert "printer a read failed, skipping poll" in caplog.text
    assert "printer b" not in caplog.text
    assert bridge.printers[1].tools[0].active == 20 and bridge.printers[0].tools == {}
    await bridge.aclose()


async def test_x16_runout_flag_clears_when_the_spool_becomes_active_elsewhere(tmp_path):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    bridge.state.runouts[19] = Runout("Tool a: unloaded mid-print with 900 g left", 1000.0, 100.0)
    unload(a)
    await bridge.poll()
    assert "unloaded mid-print" in fake.flag(19)
    # 19 goes into b, where 20 was active: another spool, then this one.
    fake.spools[20]["location"] = None
    fake.spools[19]["location"] = B
    unload(b, load=PLA)
    await bridge.poll()
    assert tools(bridge)["b"].active == 19
    assert 19 not in bridge.state.runouts
    assert "unloaded mid-print" not in (fake.flag(19) or "")  # a's open swap still flags it.
    await bridge.aclose()
