"""The harness helpers count what they say they count."""

import httpx

from tests.harness import answer, jobs, make, refused, spool


def upload(station, status, body="G28"):
    """Log a PUT the way the simulator does, `status` its answer."""
    data = {"method": "PUT", "path": "/api/v1/files/usb/x.gcode", "status": status, "body": body}
    station.sim.log(station.printer.name, "request", f"PUT x.gcode {status}", data)


def sync_job(station):
    station.sim.log(station.printer.name, "job", "sync job started", {"started": 1, "kind": "sync"})


async def test_refused_counts_the_uploads_answered_409(tmp_path):
    bridge, fake, station = make(tmp_path, [spool(19)])
    upload(station, 201)
    station.before_request = answer(409, "PUT")
    async with httpx.AsyncClient(transport=station.transport(), base_url="http://p") as client:
        resp = await client.put("/api/v1/files/usb/x.gcode", content="G28")
    assert resp.status_code == 409
    upload(station, 409)
    assert refused(station) == 2
    await bridge.aclose()


async def test_jobs_skips_an_upload_that_started_a_job_but_was_not_answered_201(tmp_path):
    bridge, fake, station = make(tmp_path, [spool(19)])
    sync_job(station)
    upload(station, 500, "first")
    sync_job(station)
    upload(station, 201, "second")
    upload(station, 201, "stored only")
    assert jobs(station) == ["second"]
    await bridge.aclose()
