"""Two printers against one Spoolman: the X scenarios in docs/multi-printer.md."""

import httpx
from test_writes import CLAIMED, jobs, ledger, restart, spool, two_printers

from spoolman_prusalink.accounting import ToolState
from spoolman_prusalink.state import Runout, load_state

A, B = "Tool a", "Tool b"


async def baseline(tmp_path, spools, **cfg):
    """A bridge with spool 19 loaded in a and spool 20 in b, both charged once."""
    bridge, fake, printers = two_printers(tmp_path, spools, **cfg)
    await bridge.poll()  # Baselines.
    for p in printers.values():
        p.extruded += 100
    await bridge.poll()
    return bridge, fake, printers


def tools(bridge):
    return {p.name: p.tools[0] for p in bridge.printers}


def unload(printer, *, load=None):
    printer.unloads += 1
    printer.at_unload = printer.extruded
    printer.material = load


async def test_x1_spoolman_first_then_unload_at_a_and_load_at_b(tmp_path):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    assert fake.used == {19: [100.0], 20: [100.0]}

    # Spoolman first: 19 moves to b's location while still loaded in a.
    fake.spools[19]["location"] = B
    a.extruded += 50
    await bridge.poll()
    st = tools(bridge)
    assert st["a"].active == 19 and st["a"].assigned is None  # S10 on a.
    assert (st["b"].active, st["b"].assigned) == (20, 19)  # S1 on b: staged.
    assert fake.used[19] == [100.0, 50.0]

    # Unload at a and load into b, within one poll interval, in either order.
    a.extruded += 10
    unload(a)
    b.extruded += 30  # b's last usage on 20.
    unload(b, load={"name": "PLA", "color": "#73737b"})
    b.extruded += 300  # The load on 19.
    await bridge.poll()
    st = tools(bridge)
    assert st["a"].active is None and st["b"].active == 19
    assert fake.used == {19: [100.0, 50.0, 10.0, 300.0], 20: [100.0, 30.0]}
    assert ("location", 20, None) in fake.writes  # b evicted 20.
    assert fake.spools[19]["location"] == B

    # a holds usage with nothing assigned, as S10 says.
    a.material = {"name": "PLA"}
    a.extruded += 40
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
    unload(b, load={"name": "PLA", "color": "#73737b"})
    b.extruded += 200
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
    b.extruded += 100
    await bridge.poll()
    st = tools(bridge)
    assert st["b"].active == 19 and st["b"].swap is None  # Confirmed on b.
    assert fake.used[20] == [100.0, 200.0, -200.0] and fake.used[19] == [100.0, 200.0, 100.0]
    assert st["a"].active is None and st["a"].swap is not None  # S3 on a: open, held.
    assert fake.flag(20) is None
    assert "Tool a: filament was unloaded, then this spool was moved out" in fake.flag(19)

    # Whatever went into a confirms a's swap and takes the usage held there.
    a.material = {"name": "PLA"}
    a.extruded += 60
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
    a.extruded += 50
    await bridge.poll()
    st = tools(bridge)
    assert st["a"].active == 19 and fake.used[19] == [100.0, 50.0]  # S10: still charged.
    assert st["b"].assigned == 19 and st["b"].active == 20  # S9: staged on b.
    b.job = 7
    await bridge.poll()
    b.extruded += 10
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
    unload(a, load={"name": "PLA", "color": "#73737b"})
    a.extruded += 100
    await bridge.poll()
    assert tools(bridge)["a"].active == 22
    assert ("location", 19, None) in fake.writes and fake.spools[19]["location"] is None
    # The user moves 19 into b's location and loads it there.
    fake.spools[19]["location"] = B
    unload(b, load={"name": "PLA", "color": "#73737b"})
    b.extruded += 100
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
    unload(a, load={"name": "PLA", "color": "#73737b"})
    a.extruded += 100

    # Between the bridge's spool read and its eviction, the user moves 19 into b's
    # location: the single-spool read sees the new location.
    handle = fake.handle

    def racing(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/spool/19":
            fake.spools[19]["location"] = B
        return handle(request)

    fake.handle = racing
    bridge.spoolman._client._transport = httpx.MockTransport(racing)
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
    a.down = True  # a is off, so it never reports the unload.
    fake.spools[20]["location"] = None
    fake.spools[19]["location"] = B
    fake.spools[19]["extra"].update(CLAIMED)
    b.extruded += 50
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
    a.down = False
    a.extruded += 10
    unload(a)
    await bridge.poll()
    st = tools(bridge)
    assert st["a"].active is None and st["b"].active == 19
    assert fake.used[19] == [100.0, 100.0, 50.0, 10.0]
    assert "at once" not in (fake.flag(19) or "")
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
    a.down = True
    # An eviction pending on a's tool doesn't need a.
    a_tools = bridge.state.tools("a")
    a_tools[0] = ToolState(**{**a_tools[0].__dict__, "evict": (25,)})
    fake.spools[25] = spool(25, A)
    b.extruded += 70
    await bridge.poll()
    assert caplog.text.count("printer a unreachable") == 1
    assert fake.used[20] == [100.0, 70.0]
    assert fake.spools[25]["location"] is None
    assert bridge.interval() == bridge.config.poll_idle
    b.job = 5
    await bridge.poll()
    assert bridge.interval() == bridge.config.poll_printing
    a.down = False
    await bridge.poll()
    assert "printer a reachable again" in caplog.text
    await bridge.aclose()


async def test_x8_an_unread_printers_flags_stay_set(tmp_path, caplog):
    # a has an unconfirmed swap flagged when it goes off. The flag stays in Spoolman and
    # isn't shown again when a comes back, including across a restart while a is off.
    bridge, fake, printers = await baseline(
        tmp_path, [spool(19, A), spool(20, B)], printer_notices=True
    )
    a, b = printers["a"], printers["b"]
    unload(a)
    await bridge.poll()
    text = "unloaded with no Spoolman change"
    assert text in fake.flag(19)
    assert len(bridge.state.noticed) == 1
    a.down = True
    b.extruded += 10
    await bridge.poll()
    assert text in fake.flag(19)
    assert len(bridge.state.noticed) == 1
    restarted = restart(bridge)
    await restarted.poll()
    assert text in fake.flag(19)
    assert len(restarted.state.noticed) == 1
    a.down = False
    shown = len(jobs(a))
    await restarted.poll()
    assert text in fake.flag(19)
    assert len(jobs(a)) == shown  # Not shown again.
    await restarted.aclose()


async def test_x9_spoolman_down_queues_both_printers_charges_in_order(tmp_path):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    fake.down = True
    a.extruded += 10
    b.extruded += 20
    await bridge.poll()
    assert fake.used == {19: [100.0], 20: [100.0]}
    fake.down = False
    fake.fail_use = [503]  # The first retry fails, so the queue is saved with both.
    await bridge.poll()
    queued = load_state(tmp_path / "state.json", ["a", "b"]).outbox
    assert [(r["printer"], r["spool_id"]) for r in queued] == [("a", 19), ("b", 20)]
    await bridge.poll()
    assert fake.used == {19: [100.0, 10.0], 20: [100.0, 20.0]}
    await bridge.aclose()


async def test_x10_unsupported_firmware_on_one_printer(tmp_path, caplog):
    bridge, fake, printers = two_printers(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    a.filament_endpoint = False
    caplog.set_level("INFO")
    await bridge.poll()
    b.extruded += 100
    await bridge.poll()
    assert caplog.text.count("printer a: ") == 1 and "isn't supported" in caplog.text
    assert fake.used == {20: [100.0]}
    # a's status still reads, so a doesn't hold the bridge at the printing rate.
    assert bridge.interval() == bridge.config.poll_idle
    a.filament_endpoint = True
    await bridge.poll()
    assert "printer a now reports filament; resuming" in caplog.text
    await bridge.aclose()


async def test_x12_notices_go_to_the_printer_whose_tool_holds_the_spool(tmp_path):
    bridge, fake, printers = await baseline(
        tmp_path, [spool(19, A), spool(20, B)], printer_notices=True
    )
    a, b = printers["a"], printers["b"]
    # A staged spool on b is flagged at b's print start; nothing is pending on a.
    fake.spools[21] = spool(21, B)
    await bridge.poll()
    b.job = 9
    await bridge.poll()
    b.extruded += 10
    await bridge.poll()  # The print starts at its first extrusion (Q4).
    b.job = None
    await bridge.poll()
    assert fake.flag(21) and "Tool b" in fake.flag(21)
    assert [g for g in jobs(b) if "M117" in g] and not jobs(a)
    # A flag a raises on a spool that's now in b's location is shown on b: a's "moved out"
    # flag after the spool left Tool a for Tool b (X2).
    unload(a)
    await bridge.poll()  # a shows its own open swap, since the spool is still in Tool a.
    assert "no Spoolman change" in fake.flag(19)
    a_jobs, b_jobs = len(jobs(a)), len(jobs(b))
    fake.spools[19]["location"] = B
    await bridge.poll()
    assert "moved out" in fake.flag(19)
    assert (len(jobs(a)), len(jobs(b))) == (a_jobs, b_jobs + 1)
    assert "M117 Spoolman: Tool a: filament was unloaded," in jobs(b)[-1]
    # The bridge's own flag on a claimed spool in no tool's location goes to the first idle
    # printer, as it does with one printer.
    fake.spools[26] = {**spool(26, "shelf"), "extra": dict(CLAIMED)}
    await bridge.poll()
    assert "isn't in a printer tool's location" in fake.flag(26)
    assert any("'Claim held usage'" in g for g in jobs(a))
    assert not any("'Claim held usage'" in g for g in jobs(b))
    await bridge.aclose()


async def test_x13_two_printers_refuse_a_single_printer_state(tmp_path):
    bridge, fake, printers = two_printers(tmp_path, [spool(19, A)])
    await bridge.poll()
    await bridge.aclose()
    path = tmp_path / "state.json"
    path.write_text(path.read_text().replace('"version": 3', '"version": 2'))
    try:
        load_state(path, ["a", "b"])
    except ValueError as e:
        assert "2 printers configured" in str(e)
    else:
        raise AssertionError("a version 2 state loaded with two printers")


async def test_read_failures_name_the_printer(tmp_path, caplog):
    bridge, fake, printers = two_printers(tmp_path, [spool(19, A), spool(20, B)])
    printers["a"].error = 401
    await bridge.poll()
    assert "printer a read failed, skipping poll" in caplog.text
    assert "printer b" not in caplog.text
    assert bridge.printers[1].tools[0].active == 20 and bridge.printers[0].tools == {}
    await bridge.aclose()


async def test_x16_runout_flag_clears_when_the_spool_becomes_active_elsewhere(tmp_path):
    bridge, fake, printers = await baseline(tmp_path, [spool(19, A), spool(20, B)])
    a, b = printers["a"], printers["b"]
    bridge.state.runouts[19] = Runout("Tool a: unloaded mid-print with 900 g left", printer="a")
    unload(a)
    await bridge.poll()
    assert "unloaded mid-print" in fake.flag(19)
    # 19 goes into b, where 20 was active: another spool, then this one.
    fake.spools[20]["location"] = None
    fake.spools[19]["location"] = B
    unload(b, load={"name": "PLA", "color": "#73737b"})
    await bridge.poll()
    assert tools(bridge)["b"].active == 19
    assert 19 not in bridge.state.runouts
    assert "unloaded mid-print" not in (fake.flag(19) or "")  # a's open swap still flags it.
    await bridge.aclose()
