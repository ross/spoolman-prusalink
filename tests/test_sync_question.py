"""A sync job that meets a question on the printer's screen: W5 and BF5.

The bridge's default preamble asks nothing on any model it writes to, so these tests
leave the input-shaper line out of it. The simulator's preview then asks about every
sync job under the printer's default check settings, and refuses it under `abort` (F27).
"""

import logging

import pytest
from harness import (
    ASKED,
    LOC,
    fill,
    jobs,
    make,
    menu,
    poll_during_job,
    spool,
    swap_to_spool_20,
)

from simulator.model import Busy, Reason, State
from simulator.preview import Severity
from spoolman_prusalink import printer_sync

DECLINED = (
    "the printer asked about the sync job writing this spool and it didn't run; check "
    "the printer's screen and its check settings: "
)


@pytest.fixture(autouse=True)
def no_input_shaper(monkeypatch):
    monkeypatch.setattr(
        printer_sync,
        "default_preamble",
        lambda m: (f'M862.3 P "{m.name}" ; printer model check', "M862.5 P2 ; g-code level check"),
    )


async def at_question(bridge, station):
    """Tick until the sync job waits at the preview's question, then poll."""
    p = station.printer
    station.tick_until(lambda: p.state is State.ATTENTION)
    assert p.job.waits_for(Reason.PREVIEW)
    await poll_during_job(bridge)


async def declined(tmp_path, checks=Severity.WARN, **cfg):
    """A swap to spool 20 whose sync job waits at the question and is then declined."""
    bridge, fake, station = make(tmp_path, [spool(19)], **cfg)
    station.printer.set_checks(checks)
    await swap_to_spool_20(bridge, fake, station)
    assert len(jobs(station)) == 1
    await at_question(bridge, station)
    assert fake.flag(20) == f"{LOC}: {ASKED}"
    station.printer.cancel()
    assert station.printer.job is None and station.printer.state is State.IDLE
    await bridge.poll()
    assert fake.flag(20).startswith(f"{LOC}: {DECLINED}")
    return bridge, fake, station


async def test_a_job_at_a_question_is_flagged_while_it_waits(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    bridge, fake, station = make(tmp_path, [spool(19)])
    await swap_to_spool_20(bridge, fake, station)
    await at_question(bridge, station)
    for _ in range(5):
        station.advance(30)
        await poll_during_job(bridge)
    assert len(jobs(station)) == 1
    assert fake.flag(20) == f"{LOC}: {ASKED}"
    assert bridge.printers[0].writes[0].asked
    assert caplog.text.count("printer is asking about the sync job for spool 20") == 1
    # The question takes over from the pending write: no line says that ended.
    assert "no longer" not in caplog.text
    await bridge.aclose()


@pytest.mark.parametrize("checks", [Severity.WARN, Severity.ABORT])
async def test_a_declined_job_is_flagged_after_one_job(tmp_path, caplog, checks):
    # Under `abort` the job waits for Abort just the same, so it's flagged the same way.
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    bridge, fake, station = await declined(tmp_path, checks)
    for _ in range(5):
        station.advance(60)
        await bridge.poll()
    assert len(jobs(station)) == 1
    assert fake.flag(20).startswith(f"{LOC}: {DECLINED}")
    assert caplog.text.count("printer is asking about the sync job for spool 20") == 1
    assert caplog.text.count("printer asked about the sync job for spool 20 and it didn't") == 1
    assert "no longer" not in caplog.text
    await bridge.aclose()


async def test_a_refused_job_offers_only_abort(tmp_path):
    bridge, fake, station = make(tmp_path, [spool(19)])
    station.printer.set_checks(Severity.ABORT)
    await swap_to_spool_20(bridge, fake, station)
    await at_question(bridge, station)
    assert fake.flag(20) == f"{LOC}: {ASKED}"
    with pytest.raises(Busy, match="offers only Abort"):
        station.printer.tap_through()
    await bridge.aclose()


async def test_an_edit_in_spoolman_after_a_decline_writes_again(tmp_path):
    bridge, fake, station = await declined(tmp_path)
    fake.spools[20]["filament"]["color_hex"] = "00ff00"
    await bridge.poll()
    assert len(jobs(station)) == 2 and 'O"#00ff00"' in jobs(station)[-1]
    # The new job asks too, and is flagged the same way.
    await at_question(bridge, station)
    assert fake.flag(20) == f"{LOC}: {ASKED}"
    station.printer.tap_through()
    station.finish_job()
    await bridge.poll()
    assert fake.flag(20) is None
    assert len(jobs(station)) == 2
    await bridge.aclose()


async def test_an_unload_and_reload_after_a_decline_writes_again(tmp_path):
    bridge, fake, station = await declined(tmp_path)
    station.tool.unload_count += 1
    station.tool.extruded_at_unload = station.tool.extruded
    fill(station.tool, None)
    await bridge.poll()
    assert len(jobs(station)) == 1
    fill(station.tool, menu("PLA"))
    await bridge.poll()
    assert len(jobs(station)) == 2
    await bridge.aclose()


async def test_print_at_the_question_lets_the_write_land(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="spoolman_prusalink")
    bridge, fake, station = make(tmp_path, [spool(19)])
    await swap_to_spool_20(bridge, fake, station)
    await at_question(bridge, station)
    assert fake.flag(20) == f"{LOC}: {ASKED}"
    station.printer.tap_through()
    station.finish_job()
    await bridge.poll()
    assert fake.flag(20) is None
    assert not bridge.printers[0].writes[0].asked
    assert len(jobs(station)) == 1
    assert caplog.text.count("no longer: printer is asking about the sync job") == 1
    await bridge.aclose()


async def test_a_running_job_after_the_question_clears_asked(tmp_path):
    # A poll while the job prints after Print, before it ends, sees it past the question.
    bridge, fake, station = make(tmp_path, [spool(19)])
    await swap_to_spool_20(bridge, fake, station)
    await at_question(bridge, station)
    p = station.printer
    p.tap_through()
    station.tick_until(lambda: p.state is State.PRINTING)
    await poll_during_job(bridge)
    assert not bridge.printers[0].writes[0].asked
    station.finish_job()
    await bridge.poll()
    assert fake.flag(20) is None
    await bridge.aclose()


async def test_an_edit_after_a_write_that_didnt_take_writes_again(tmp_path):
    # W5 without a question: two jobs that change nothing, then an edit in Spoolman.
    bridge, fake, station = make(tmp_path, [spool(19)])
    station.printer.set_checks(Severity.IGNORE)
    station.printer.set_stuck_write(0, True)  # The job runs but changes nothing.
    await swap_to_spool_20(bridge, fake, station)
    for _ in range(4):
        station.finish_job()
        await bridge.poll()
    assert len(jobs(station)) == 2
    assert "didn't take" in fake.flag(20)
    station.printer.set_stuck_write(0, False)
    fake.spools[20]["filament"]["color_hex"] = "00ff00"
    await bridge.poll()
    assert len(jobs(station)) == 3 and 'O"#00ff00"' in jobs(station)[-1]
    station.finish_job()
    await bridge.poll()
    assert fake.flag(20) is None
    await bridge.aclose()
