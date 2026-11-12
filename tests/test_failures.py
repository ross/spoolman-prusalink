"""Failure, recovery and validation paths in the bridge: what is logged once, what is left alone."""

import asyncio
import json
import logging
from dataclasses import replace

import httpx
import pytest
from test_writes import (
    LOC,
    jobs,
    levels,
    make,
    restart,
    spool,
    spool_x,
    swap_to_spool_20,
    two_printers,
)

from spoolman_prusalink.accounting import EventKind
from spoolman_prusalink.db import read_state
from spoolman_prusalink.errors import BadAnswer

TAG = "91da34d9-c1c1-470f-bac7-185a2346f86a"


def count(caplog, text):
    return caplog.text.count(text)


def records(caplog):
    """(level, message) of each record at INFO and above."""
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.levelno >= logging.INFO]


def records_after(caplog, start):
    """(level, message) of each record at INFO and above, from index `start` on."""
    return [
        (r.levelname, r.getMessage()) for r in caplog.records[start:] if r.levelno >= logging.INFO
    ]


def stored_uuid(fake, spool_id):
    return json.loads(fake.spools[spool_id]["extra"]["instance_uuid"])


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("no route"), ValueError("not json")], ids=["http", "json"]
)
async def test_unreadable_printer_model_keeps_writes_off_and_warns_once(
    tmp_path, caplog, monkeypatch, error
):
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    client = bridge.printers[0].client
    real = client.printer_type
    caplog.set_level("INFO")

    async def broken():
        raise error

    monkeypatch.setattr(client, "printer_type", broken)
    await swap_to_spool_20(bridge, fake, printer)  # Four polls.
    await bridge.poll()
    assert jobs(printer) == []
    assert "instance_uuid" not in fake.spools[20]["extra"]  # Nothing minted either.
    assert f"can't read the printer model ({error}); printer writes are off" in caplog.text
    assert count(caplog, "printer writes are off until it can") == 1
    assert f"{LOC}: can't write to the printer: can't read the printer model" in fake.flag(20)

    # A different error is news.
    async def other():
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(client, "printer_type", other)
    await bridge.poll()
    assert count(caplog, "printer writes are off until it can") == 2

    monkeypatch.setattr(client, "printer_type", real)
    await bridge.poll()
    # Writes coming back on ends the warning, so it's logged at the warning's level.
    assert levels(caplog, "printer is COREONEL (8.1.0)") == ["WARNING"]
    await bridge.poll()
    assert len(jobs(printer)) == 1
    await bridge.aclose()


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("printer refused"), ValueError("bad json")], ids=["http", "json"]
)
async def test_sync_job_failing_without_a_status_is_logged_once_and_retried(
    tmp_path, caplog, monkeypatch, error
):
    bridge, fake, printer = make(tmp_path, [spool(19)], uuid_field="instance_uuid")
    client = bridge.printers[0].client
    real = client.upload_and_print
    caplog.set_level("INFO")

    async def broken(*args):
        raise error

    monkeypatch.setattr(client, "upload_and_print", broken)
    await swap_to_spool_20(bridge, fake, printer)
    assert jobs(printer) == []
    await bridge.poll()  # Still broken: the same failure isn't logged again.
    assert count(caplog, f"sync job failed: {error}") == 1
    assert count(caplog, "sync job working again") == 0

    monkeypatch.setattr(client, "upload_and_print", real)
    await bridge.poll()
    assert len(jobs(printer)) == 1
    assert count(caplog, "sync job failed") == 1
    assert levels(caplog, "sync job working again") == ["WARNING"]
    await bridge.aclose()


async def test_sync_job_busy_and_failing_ends_with_one_line(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.poll()
    printer_bridge = bridge.printers[0]
    # A 409 left the job waiting, then a later attempt failed outright.
    printer_bridge._sync_busy, printer_bridge._sync_failed = "409", "500"
    caplog.clear()
    await bridge.poll()  # Nothing to write any more.
    assert levels(caplog, "sync job no longer needed") == ["WARNING"]

    printer_bridge._sync_busy = "409"
    caplog.clear()
    await bridge.poll()
    assert levels(caplog, "sync job no longer needed") == ["INFO"]

    printer_bridge._sync_delaying = True  # A delay wait ends without a line.
    caplog.clear()
    await bridge.poll()
    await bridge.poll()
    assert count(caplog, "sync job no longer needed") == 0
    await bridge.aclose()


async def test_evicted_spool_that_already_left_clears_the_eviction_problem(tmp_path, caplog):
    bridge, fake, printers = two_printers(tmp_path, [spool(19, "Tool a"), spool(20, "Tool b")])
    a = printers["a"]
    caplog.set_level("INFO")
    await bridge.poll()
    fake.spools[22] = spool(22, "Tool a")  # Spoolman first on a: 19 goes out.
    await bridge.poll()
    a.unloads, a.at_unload, a.material = 1, 1000, {"name": "PLA", "color": "#73737b"}
    fake.fail_patch = 500
    await bridge.poll()
    tool = bridge.printers[0].tools[0]
    assert tool.evict == (19,)
    assert count(caplog, "can't move spool 19 out") == 1

    # With a unreachable, its accounting doesn't run to drop the eviction. The user moves
    # the spool out themselves meanwhile, so the bridge finds nothing left to do.
    a.down = True
    fake.fail_patch = None  # A move would succeed, so it not happening is the bridge's doing.
    fake.spools[19]["location"] = "Shelf"
    await bridge.poll()
    assert bridge.printers[0].tools[0].evict == (19,)
    assert levels(caplog, "[Tool a] spool 19 is out of the location now") == ["INFO"]
    assert fake.spools[19]["location"] == "Shelf"
    assert [w for w in fake.writes if w[0] == "location"] == []
    assert count(caplog, "can't move spool 19 out") == 1
    assert levels(caplog, "[Tool a] spool 19 is out of the location now") == ["INFO"]
    await bridge.aclose()


@pytest.fixture
def uuid_bridge(tmp_path):
    return make(tmp_path, [spool(19), spool(20)], uuid_field="instance_uuid")


async def test_failed_uuid_field_setup_mints_nothing(uuid_bridge, caplog):
    bridge, fake, printer = uuid_bridge
    caplog.set_level("INFO")
    fake.fail_field = 500
    target = (await bridge.spoolman.spools())[0]
    assert await bridge._mint_uuid(target) is None
    assert await bridge._mint_uuid(target) is None
    assert count(caplog, "can't set up the 'instance_uuid' spool field") == 1
    assert fake.writes == [] and "instance_uuid" not in fake.spools[19]["extra"]

    fake.fail_field = None
    minted = await bridge._mint_uuid(target)
    assert minted and stored_uuid(fake, 19) == minted
    assert count(caplog, "'instance_uuid' spool field is set up") == 1
    await bridge.aclose()


async def test_uuid_field_of_another_type_is_known_until_set_up(uuid_bridge, caplog):
    bridge, fake, printer = uuid_bridge
    caplog.set_level("INFO")
    fake.fields.append({"key": "instance_uuid", "name": "UUID", "field_type": "integer"})
    target = (await bridge.spoolman.spools())[0]
    assert await bridge._mint_uuid(target) is None
    assert bridge._unusable_fields == {"instance_uuid"}

    fake.fields = []  # Someone deletes the field, so the bridge can set it up.
    assert await bridge._mint_uuid(target)
    assert bridge._unusable_fields == set()
    assert count(caplog, "'instance_uuid' spool field is set up") == 1
    await bridge.aclose()


async def test_failed_uuid_field_setup_adopts_nothing(uuid_bridge, caplog):
    bridge, fake, printer = uuid_bridge
    caplog.set_level("INFO")
    fake.fail_field = 500
    assert await bridge._adopt_uuid(19, TAG) is False
    assert await bridge._adopt_uuid(19, TAG) is False
    assert count(caplog, "can't set up the 'instance_uuid' spool field") == 1
    assert fake.writes == []

    fake.fail_field = None
    assert await bridge._adopt_uuid(19, TAG) is True
    assert stored_uuid(fake, 19) == TAG
    await bridge.aclose()


async def test_failed_archived_check_adopts_nothing_and_logs_once(uuid_bridge, caplog):
    bridge, fake, printer = uuid_bridge
    caplog.set_level("INFO")
    fake.fail_read = 500
    assert await bridge._adopt_uuid(19, TAG) is False
    assert await bridge._adopt_uuid(19, TAG) is False
    assert count(caplog, "can't check spool 19's tag UUID") == 1
    assert fake.writes == []
    assert (19, TAG) not in bridge._refused_uuids  # Not a refusal: it's tried again.

    assert levels(caplog, "can't check spool 19's tag UUID") == ["WARNING"]
    fake.fail_read = None
    caplog.clear()
    assert await bridge._adopt_uuid(19, TAG) is True
    assert stored_uuid(fake, 19) == TAG
    assert records(caplog) == [("INFO", f"gave spool 19 its tag's instance UUID {TAG}")]
    await bridge.aclose()


async def test_failed_check_of_a_kept_uuid_logs_its_recovery(uuid_bridge, caplog):
    bridge, fake, printer = uuid_bridge
    caplog.set_level("INFO")
    fake.fail_read = 500
    assert await bridge._adopt_uuid(19, TAG, store=False) is False
    fake.fail_read = None
    caplog.clear()
    assert await bridge._adopt_uuid(19, TAG, store=False) is True
    assert records(caplog) == [
        ("INFO", f"checked spool 19's tag UUID {TAG}: no other spool has it")
    ]
    assert fake.writes == []
    await bridge.aclose()


async def test_failed_store_of_an_adopted_uuid_logs_once_and_retries(uuid_bridge, caplog):
    bridge, fake, printer = uuid_bridge
    caplog.set_level("INFO")
    fake.fail_patch = 500
    assert await bridge._adopt_uuid(19, TAG) is False
    assert await bridge._adopt_uuid(19, TAG) is False
    assert count(caplog, "can't store an instance UUID on spool 19") == 1
    assert "instance_uuid" not in fake.spools[19]["extra"]

    assert levels(caplog, "can't store an instance UUID on spool 19") == ["WARNING"]
    fake.fail_patch = None
    caplog.clear()
    assert await bridge._adopt_uuid(19, TAG) is True
    assert stored_uuid(fake, 19) == TAG
    assert records(caplog) == [("INFO", f"gave spool 19 its tag's instance UUID {TAG}")]
    await bridge.aclose()


async def test_failed_store_of_a_minted_uuid_logs_once_and_retries(uuid_bridge, caplog):
    bridge, fake, printer = uuid_bridge
    caplog.set_level("INFO")
    target = (await bridge.spoolman.spools())[0]
    fake.fail_patch = 500
    assert await bridge._mint_uuid(target) is None
    assert await bridge._mint_uuid(target) is None
    assert levels(caplog, "can't store an instance UUID on spool 19") == ["WARNING"]
    fake.fail_patch = None
    caplog.clear()
    minted = await bridge._mint_uuid(target)
    assert minted and stored_uuid(fake, 19) == minted
    assert records(caplog) == [("INFO", f"gave spool 19 instance UUID {minted}")]
    await bridge.aclose()


async def test_printer_that_doesnt_report_a_configured_tool_is_noted_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19), spool(21, location="Tool 2")])
    printer_bridge = bridge.printers[0]
    printer_bridge.config = replace(printer_bridge.config, tools={0: LOC, 1: "Tool 2"})
    caplog.set_level("INFO")
    await bridge.poll()
    printer.extruded = 1500
    for _ in range(3):
        await bridge.poll()
    assert count(caplog, "[Tool 2] printer doesn't report tool index 1") == 1
    assert fake.used == {19: [500.0]}  # The tool it does report is charged.
    assert 1 not in printer_bridge.tools
    await bridge.aclose()


async def test_a_tool_note_that_goes_away_is_logged_once_at_its_level(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    printer_bridge = bridge.printers[0]
    gone = ("index", "[Tool 2] printer doesn't report tool index 1")
    kept = ("other", "[Tool 2] another note")
    printer_bridge._notes(1, [gone, kept])
    printer_bridge._notes(1, [kept])  # Only the note that went away ends.
    printer_bridge._notes(1, [kept])
    printer_bridge._notes(1, [])
    printer_bridge._notes(1, [])
    ends = [(r.levelname, r.getMessage()) for r in caplog.records if "no longer" in r.getMessage()]
    assert ends == [
        ("WARNING", "[Tool 2] no longer: printer doesn't report tool index 1"),
        ("WARNING", "[Tool 2] no longer: another note"),
    ]
    caplog.clear()
    printer_bridge._notes(1, [gone])  # Back again: logged afresh.
    assert [r.getMessage() for r in caplog.records] == [gone[1]]
    await bridge.aclose()


async def test_a_tool_note_with_new_detail_is_logged_without_an_end_line(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    printer_bridge = bridge.printers[0]
    what = ("printer", 19)
    printer_bridge._notes(0, [(what, f"[{LOC}] printer differs from spool 19: color")])
    printer_bridge._notes(0, [(what, f"[{LOC}] printer differs from spool 19: color; name")])
    assert [r.getMessage() for r in caplog.records] == [
        f"[{LOC}] printer differs from spool 19: color",
        f"[{LOC}] printer differs from spool 19: color; name",
    ]
    # A poll that doesn't compare the printer with the spool leaves the note standing.
    caplog.clear()
    printer_bridge._notes(0, [(EventKind.AMBIGUOUS, f"[{LOC}] location holds spools")], False)
    printer_bridge._notes(0, [], compared=False)
    assert [r.getMessage() for r in caplog.records] == [
        f"[{LOC}] location holds spools",
        f"[{LOC}] no longer: location holds spools",
    ]
    caplog.clear()
    printer_bridge._notes(0, [])
    assert [r.getMessage() for r in caplog.records] == [
        f"[{LOC}] no longer: printer differs from spool 19: color; name"
    ]
    await bridge.aclose()


async def test_eviction_no_longer_needed_logs_only_after_a_failure(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("INFO")
    await bridge.evict([])  # Spool 19 isn't in any tool's evict list, and never failed.
    assert count(caplog, "no longer needs moving out") == 0
    bridge._evict_failing[19] = "500"
    await bridge.evict([])
    await bridge.evict([])
    assert levels(caplog, "spool 19 no longer needs moving out") == ["INFO"]
    await bridge.aclose()


async def test_evict_failing_then_moved_logs_warning_then_info(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("DEBUG", "spoolman_prusalink")
    await bridge.poll()
    fake.spools[20] = spool_x(20)
    await bridge.poll()
    printer.unloads, printer.at_unload, printer.material = 1, 1000, None
    fake.fail_patch = 500
    for _ in range(3):
        await bridge.poll()
    assert levels(caplog, "move spool 19 out") == ["WARNING", "DEBUG", "DEBUG"]
    fake.fail_patch = 503  # A different error is logged again.
    await bridge.poll()
    assert levels(caplog, "move spool 19 out")[-1] == "WARNING"
    fake.fail_patch = None
    await bridge.poll()
    assert levels(caplog, "moved spool 19 to no location") == ["INFO"]
    await bridge.aclose()


async def test_flagged_spools_missing_from_the_list_are_looked_up(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19, flag="still wanted")])
    fake.spools[19]["archived"] = True
    bridge.state.flagged[:] = [19, 20]
    bridge._save()
    bridge._flags = {19: "still wanted"}  # 20 has no flag wanted and was deleted.
    await bridge.sync_flags([])
    assert bridge.state.flagged == [19]  # 19 is archived and keeps its flag; 20 is forgotten.
    assert read_state(tmp_path / "bridge.db").flagged == [19]
    assert [w for w in fake.writes if w[0] == "patch"] == []
    assert sorted(fake.lookups) == [19, 20]
    await bridge.sync_flags([])  # The archived spool is kept, so it isn't looked up again.
    assert sorted(fake.lookups) == [19, 20]
    await bridge.aclose()


async def test_flag_wanted_on_an_unlisted_spool_the_bridge_never_flagged_is_left(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    bridge._flags = {19: "wanted"}  # Not in the list given, and never flagged.
    await bridge.sync_flags([])
    assert bridge.state.flagged == []
    assert fake.lookups == []
    assert [w for w in fake.writes if w[0] == "patch"] == []
    await bridge.aclose()


async def test_flagged_spool_unarchived_since_the_list_was_read_is_kept_current(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19, flag="x")])
    fake.unlisted.add(19)
    bridge.state.flagged[:] = [19]
    bridge._flags = {}
    await bridge.sync_flags(await bridge.spoolman.spools())
    assert fake.flag(19) is None
    assert bridge.state.flagged == []
    assert bridge._archived == {}  # Not archived, so the next list has it.
    await bridge.aclose()


@pytest.mark.parametrize("queued", [False, True])
async def test_run_polls_until_stopped(tmp_path, caplog, queued):
    bridge, fake, printer = make(tmp_path, [spool(19)], poll_idle=0, poll_printing=0)
    await bridge.poll()
    if queued:
        printer.extruded = 1500
        fake.fail_use = [503]
        await bridge.poll()
        bridge = restart(bridge)  # Loads the queued charge.
        assert len(bridge.state.outbox) == 1
    caplog.set_level("INFO")
    stop, polls = asyncio.Event(), []
    poll = bridge.poll

    async def stopping():
        polls.append(1)
        await poll()
        stop.set()

    bridge.poll = stopping
    await asyncio.wait_for(bridge.run(stop), timeout=5)
    assert len(polls) == 1
    assert count(caplog, "starting: printer=http://printer") == 1
    assert count(caplog, "1 charge(s) queued from before the restart") == int(queued)
    assert fake.used == ({19: [500.0]} if queued else {})
    assert bridge.state.outbox == []
    await bridge.aclose()


async def test_cleared_flag_leaves_the_flagged_list_and_is_saved(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19, flag="was flagged"), spool(20)])
    bridge.state.flagged[:] = [19]
    bridge._save()  # As an earlier poll left it.
    bridge._flags = {}  # Nothing wanted any more.
    await bridge.sync_flags(await bridge.spoolman.spools())
    assert fake.flag(19) is None
    assert bridge.state.flagged == []
    assert read_state(tmp_path / "bridge.db").flagged == []
    await bridge.aclose()


@pytest.mark.parametrize("how", ["deleted", "archived"])
async def test_flagged_spool_that_leaves_the_list_is_forgotten_and_saved(tmp_path, how):
    bridge, fake, printer = make(tmp_path, [spool(19, flag="x"), spool(20)])
    bridge.state.flagged[:] = [19]
    bridge._save()  # As an earlier poll left it.
    bridge._flags = {}
    if how == "deleted":
        del fake.spools[19]
    else:
        fake.spools[19]["archived"] = True
    await bridge.sync_flags(await bridge.spoolman.spools())
    assert bridge.state.flagged == []
    assert read_state(tmp_path / "bridge.db").flagged == []
    # An archived spool's stale flag is cleared (#120); a deleted one has none.
    patches = [w for w in fake.writes if w[0] == "patch"]
    if how == "deleted":
        assert patches == []
    else:
        assert patches == [("patch", 19)]
        assert fake.flag(19) is None
    await bridge.aclose()


@pytest.mark.parametrize("how", ["deleted", "archived"])
async def test_flagged_spool_that_leaves_the_list_while_its_flag_fails_ends_once(
    tmp_path, caplog, how
):
    # #120: the failure's end is logged once at INFO, and an archived spool's stale flag
    # is cleared once writes work again.
    bridge, fake, printer = make(tmp_path, [spool(19, flag="x"), spool(20)])
    caplog.set_level("DEBUG", "spoolman_prusalink")
    bridge.state.flagged[:] = [19]
    bridge._flags = {}
    fake.fail_patch = 500
    for _ in range(2):
        await bridge.sync_flags(await bridge.spoolman.spools())
    assert levels(caplog, "can't update the flag on spool 19") == ["WARNING", "DEBUG"]

    before = len(caplog.records)
    if how == "deleted":
        del fake.spools[19]  # Its lookup works, so this ends the failure at once.
    else:
        fake.spools[19]["archived"] = True
    # Writes still fail: the archived spool keeps its flag and is retried, quietly.
    await bridge.sync_flags(await bridge.spoolman.spools())
    if how == "archived":
        assert fake.flag(19) == "x"
        assert bridge.state.flagged == [19]
        assert levels(caplog, "can't update the flag on spool 19")[-1] == "DEBUG"
    fake.fail_patch = None
    for _ in range(3):
        await bridge.sync_flags(await bridge.spoolman.spools())
    ended = records_after(caplog, before)
    if how == "deleted":
        assert ended == [("INFO", "spool 19 was deleted; its flag no longer needs updating")]
    else:
        assert ended == [("INFO", "cleared flag on spool 19")]
        assert fake.flag(19) is None
    assert bridge.state.flagged == []
    assert bridge._flag_failing == {}
    assert levels(caplog, "can't update the flag on spool 19").count("WARNING") == 1
    await bridge.aclose()


async def test_flagged_spool_that_cant_be_looked_up_is_retried_and_logged_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19, flag="x")])
    caplog.set_level("DEBUG", "spoolman_prusalink")
    fake.spools[19]["archived"] = True
    bridge.state.flagged[:] = [19]
    bridge._flags = {}
    fake.fail_lookup = 500
    for _ in range(2):
        await bridge.sync_flags([])
    assert levels(caplog, "can't look up flagged spool 19") == ["WARNING", "DEBUG"]
    assert bridge.state.flagged == [19]
    fake.fail_lookup = None
    before = len(caplog.records)
    await bridge.sync_flags([])
    assert records_after(caplog, before) == [("INFO", "cleared flag on spool 19")]
    assert fake.flag(19) is None
    await bridge.aclose()


async def test_archived_flagged_spool_deleted_while_its_flag_fails_ends_once(tmp_path, caplog):
    # Once looked up, an archived spool is cached and not looked up again, so a 404 on its
    # flag write is what shows it was deleted.
    bridge, fake, printer = make(tmp_path, [spool(19, flag="x"), spool(20)])
    caplog.set_level("DEBUG", "spoolman_prusalink")
    bridge.state.flagged[:] = [19]
    bridge._flags = {}
    fake.spools[19]["archived"] = True
    fake.fail_patch = 500
    for _ in range(2):
        await bridge.sync_flags(await bridge.spoolman.spools())
    assert 19 in bridge._archived
    assert levels(caplog, "can't update the flag on spool 19") == ["WARNING", "DEBUG"]

    del fake.spools[19]
    fake.fail_patch = None
    before = len(caplog.records)
    for _ in range(3):
        await bridge.sync_flags(await bridge.spoolman.spools())
    assert records_after(caplog, before) == [
        ("INFO", "spool 19 was deleted; its flag no longer needs updating")
    ]
    assert bridge.state.flagged == []
    assert read_state(tmp_path / "bridge.db").flagged == []
    assert bridge._archived == {}
    assert bridge._flag_failing == {}
    assert fake.lookups == [19]  # Looked up once, while archived.
    assert levels(caplog, "can't update the flag on spool 19") == ["WARNING", "DEBUG"]
    await bridge.aclose()


async def test_archived_active_spool_deleted_while_its_flag_fails_ends_once(tmp_path, caplog):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    caplog.set_level("DEBUG", "spoolman_prusalink")
    await bridge.poll()
    fake.spools[19]["archived"] = True
    fake.fail_patch = 500  # Setting the "archived but still loaded" flag fails.
    for _ in range(2):
        await bridge.poll()
    assert 19 in bridge._archived
    assert levels(caplog, "can't update the flag on spool 19") == ["WARNING", "DEBUG"]
    assert fake.lookups == [19]

    del fake.spools[19]
    fake.fail_patch = None
    before = len(caplog.records)
    await bridge.poll()
    # The 404 ends the failure and drops the cached spool, so the next poll looks it up
    # and finds it deleted.
    assert records_after(caplog, before) == [
        ("INFO", "spool 19 was deleted; its flag no longer needs updating")
    ]
    assert bridge._archived == {}
    await bridge.poll()
    assert fake.lookups == [19, 19]
    assert bridge.printers[0].tools[0].active is None
    before = len(caplog.records)
    for _ in range(2):
        await bridge.poll()
    assert records_after(caplog, before) == []
    assert bridge._flag_failing == {}
    assert levels(caplog, "can't update the flag on spool 19") == ["WARNING", "DEBUG"]
    await bridge.aclose()


async def test_error_page_that_changes_each_time_is_logged_once(tmp_path, caplog, monkeypatch):
    """A proxy's error page with a request ID in it isn't a new failure on every poll."""
    bridge, fake, printer = make(tmp_path, [spool(19)])
    await bridge.poll()
    caplog.set_level("INFO")
    calls = {"n": 0}

    def gateway(path):
        calls["n"] += 1
        summary = f"502 Bad Gateway for {path}"
        return BadAnswer(f"{summary}: request {calls['n']}", 502, summary)

    async def use_length(spool_id, mm):
        raise gateway("PUT /api/v1/spool/19/use")

    async def spools(archived=False):
        raise gateway("GET /api/v1/spool")

    real = bridge.spoolman.use_length
    monkeypatch.setattr(bridge.spoolman, "use_length", use_length)
    printer.extruded = 1500
    for _ in range(3):  # The charge is queued, and the next polls can't send it.
        await bridge.poll()
    assert count(caplog, "charge to spool 19 failed (502 Bad Gateway") == 1
    assert "failed (502 Bad Gateway for PUT /api/v1/spool/19/use: request 1)" in caplog.text

    monkeypatch.setattr(bridge.spoolman, "use_length", real)
    monkeypatch.setattr(bridge.spoolman, "spools", spools)
    for _ in range(3):
        await bridge.poll()
    assert count(caplog, "Spoolman read failed") == 1
    assert "skipping poll: 502 Bad Gateway for GET /api/v1/spool: request 4" in caplog.text
    await bridge.aclose()
