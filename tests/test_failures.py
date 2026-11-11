"""Failure, recovery and validation paths in the bridge: what is logged once, what is left alone."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from test_writes import LOC, jobs, make, restart, spool, swap_to_spool_20, two_printers

from spoolman_prusalink.db import read_state

TAG = "91da34d9-c1c1-470f-bac7-185a2346f86a"


def count(caplog, text):
    return caplog.text.count(text)


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
    assert "printer is COREONEL (8.1.0)" in caplog.text
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
    assert count(caplog, "sync job working again") == 1
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
    assert ("evict", 19) in bridge._problems._seen

    # With a unreachable, its accounting doesn't run to drop the eviction. The user moves
    # the spool out themselves meanwhile, so the bridge finds nothing left to do.
    a.down = True
    fake.fail_patch = None  # A move would succeed, so it not happening is the bridge's doing.
    fake.spools[19]["location"] = "Shelf"
    await bridge.poll()
    assert bridge.printers[0].tools[0].evict == (19,)
    assert ("evict", 19) not in bridge._problems._seen
    assert fake.spools[19]["location"] == "Shelf"
    assert [w for w in fake.writes if w[0] == "location"] == []
    assert count(caplog, "can't move spool 19 out") == 1
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

    fake.fail_read = None
    assert await bridge._adopt_uuid(19, TAG) is True
    assert stored_uuid(fake, 19) == TAG
    assert ("uuid", 19) not in bridge._problems._seen
    await bridge.aclose()


async def test_failed_store_of_an_adopted_uuid_logs_once_and_retries(uuid_bridge, caplog):
    bridge, fake, printer = uuid_bridge
    caplog.set_level("INFO")
    fake.fail_patch = 500
    assert await bridge._adopt_uuid(19, TAG) is False
    assert await bridge._adopt_uuid(19, TAG) is False
    assert count(caplog, "can't store an instance UUID on spool 19") == 1
    assert "instance_uuid" not in fake.spools[19]["extra"]
    assert ("uuid", 19) in bridge._problems._seen

    fake.fail_patch = None
    assert await bridge._adopt_uuid(19, TAG) is True
    assert stored_uuid(fake, 19) == TAG
    assert ("uuid", 19) not in bridge._problems._seen
    assert count(caplog, "gave spool 19 its tag's instance UUID") == 1
    await bridge.aclose()


async def test_dry_run_logs_once_that_a_spool_would_take_its_tags_uuid(uuid_bridge, caplog):
    bridge, fake, printer = uuid_bridge
    bridge.config = replace(bridge.config, dry_run=True)
    caplog.set_level("INFO")
    assert await bridge._adopt_uuid(19, TAG) is True
    assert await bridge._adopt_uuid(19, TAG) is True
    assert count(caplog, f"would give spool 19 its tag's instance UUID {TAG}") == 1
    other = "44c406a5-c6f5-4440-bfba-e8060564b717"
    assert await bridge._adopt_uuid(19, other) is True  # A different tag is news.
    assert count(caplog, "would give spool 19 its tag's instance UUID") == 2
    assert fake.writes == [] and fake.fields == []
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


async def test_flag_wanted_on_a_spool_missing_from_the_list_stays_flagged(tmp_path):
    bridge, fake, printer = make(tmp_path, [spool(19)])
    bridge.state.flagged[:] = [19, 20]
    bridge._save()
    bridge._flags = {19: "still wanted"}  # 20 has no flag wanted and isn't in the list.
    await bridge.sync_flags([])
    assert bridge.state.flagged == [19]  # 19 may be back; 20 is forgotten.
    assert read_state(tmp_path / "bridge.db").flagged == [19]
    assert [w for w in fake.writes if w[0] == "patch"] == []
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
    assert [w for w in fake.writes if w[0] == "patch"] == []  # Nothing to write to it.
    await bridge.aclose()
