"""The PrusaLink clone over HTTP, as the bridge calls it."""

from __future__ import annotations

import asyncio

import aiohttp
import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from simulator import prusalink
from simulator.model import PrintSpec, Segment, SimTool, State
from simulator.tests.util import KEY, PLA, make

AUTH = {"X-Api-Key": KEY}
UPLOAD = {**AUTH, "Print-After-Upload": "?1", "Overwrite": "?1", "Content-Type": "text/x.gcode"}
JOB = 'M862.3 P "COREONEL"\nM865 X R N"PETG" O"#1f6fb2" L0\nM117 Spoolman: test\n'


@pytest.fixture
async def setup():
    sim, printer = make()
    client = TestClient(TestServer(prusalink.make_app(sim, "p")))
    await client.start_server()
    yield sim, printer, client
    await client.close()


async def test_version_status_job_filament(setup):
    sim, p, client = setup
    resp = await client.get("/api/version", headers=AUTH)
    assert resp.status == 200
    assert (await resp.json())["printer"] == "8.1.0"

    resp = await client.get("/api/v1/status", headers=AUTH)
    data = await resp.json()
    assert data["printer"]["state"] == "IDLE" and "job" not in data
    assert "axis_x" in data["printer"]

    resp = await client.get("/api/v1/job", headers=AUTH)
    assert resp.status == 204

    resp = await client.get("/api/v1/filament", headers=AUTH)
    data = await resp.json()
    assert data["boot_id"] == p.boot_id
    assert data["tools"][0]["material"]["instance_uuid"] == PLA.instance_uuid

    p.set_firmware("no_endpoint")
    resp = await client.get("/api/v1/filament", headers=AUTH)
    assert resp.status == 404

    resp = await client.get("/api/v1/nothing", headers=AUTH)
    assert resp.status == 404 and await resp.json() == {"message": "Not Found"}
    assert (await client.get("/api/v1/files/usb", headers=AUTH)).status == 404
    assert (await client.post("/api/v1/status", headers=AUTH)).status == 405
    assert (await client.get("/api/v1/files/usb/a.gcode", headers=AUTH)).status == 405


async def test_requests_are_logged(setup):
    sim, p, client = setup
    await client.get("/api/v1/status", headers=AUTH)
    await client.put("/api/v1/files/usb/a.gcode", data="M117 x\n", headers=UPLOAD)
    requests = [e for e in sim.events if e.kind == "request"]
    assert requests[0].text == "GET /api/v1/status 200"
    assert requests[1].text == "PUT /api/v1/files/usb/a.gcode 201"
    assert requests[1].data == {"body": "M117 x\n"}


async def test_bad_key_and_faults(setup):
    sim, p, client = setup
    resp = await client.get("/api/v1/status", headers={"X-Api-Key": "wrong"})
    assert resp.status == 401
    assert (await client.get("/api/v1/status")).status == 401
    p.set_fault("bad_key")
    assert (await client.get("/api/v1/status", headers=AUTH)).status == 401
    p.set_fault("error")
    assert (await client.get("/api/v1/status", headers=AUTH)).status == 500
    p.set_fault("none")
    assert (await client.get("/api/v1/status", headers=AUTH)).status == 200


async def test_hung_printer_never_answers(setup):
    sim, p, client = setup
    p.set_power("hung")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.get("/api/v1/status", headers=AUTH), 0.5)
    p.set_power("on")


async def test_sync_job_upload_runs_to_finished(setup):
    sim, p, client = setup
    resp = await client.put("/api/v1/files/usb/spoolman.gcode", data=JOB, headers=UPLOAD)
    assert resp.status == 201
    info = await resp.json()
    assert info["path"] == "/usb/spoolman.gcode" and info["size"] == len(JOB)

    status = await (await client.get("/api/v1/status", headers=AUTH)).json()
    assert status["printer"]["state"] == "PRINTING" and status["job"]["id"] == 1
    job = await (await client.get("/api/v1/job", headers=AUTH)).json()
    assert job["file"]["display_name"] == "spoolman.gcode"

    sim.tick(0.8)
    filament = await (await client.get("/api/v1/filament", headers=AUTH)).json()
    assert filament["tools"][0]["material"] == {
        "name": "PETG",
        "nozzle_temperature": 215,
        "preheat_temperature": 170,
        "bed_temperature": 60,
        "color": "#1f6fb2",
    }
    sim.tick(10)
    status = await (await client.get("/api/v1/status", headers=AUTH)).json()
    assert status["printer"]["state"] == "FINISHED" and "job" not in status
    assert (await client.get("/api/v1/job", headers=AUTH)).status == 204


async def test_upload_refusals(setup):
    sim, p, client = setup
    resp = await client.put("/api/v1/files/sd/a.gcode", data="x", headers=UPLOAD)
    assert resp.status == 403

    headers = {**AUTH, "Print-After-Upload": "?1"}
    assert (await client.put("/api/v1/files/usb/a.gcode", data="x", headers=headers)).status == 201
    sim.tick(100)
    resp = await client.put("/api/v1/files/usb/a.gcode", data="x", headers=headers)
    assert resp.status == 409
    assert (await resp.json())["message"] == "File already exists"

    p.unload(0)  # BUSY
    resp = await client.put("/api/v1/files/usb/b.gcode", data="y", headers=UPLOAD)
    assert resp.status == 409
    assert (await resp.json())["message"] == "Can't print right now"
    assert p.files["b.gcode"] == "y"


async def test_model_check_over_http(setup):
    sim, p, client = setup
    body = JOB.replace("COREONEL", "MK4S")
    await client.put("/api/v1/files/usb/s.gcode", data=body, headers=UPLOAD)
    sim.tick(0.8)
    status = await (await client.get("/api/v1/status", headers=AUTH)).json()
    assert status["printer"]["state"] == "ATTENTION" and "job" not in status
    assert (await client.get("/api/v1/job", headers=AUTH)).status == 204
    p.tap_through()
    sim.tick(10)
    assert p.state is State.FINISHED and p.tools[0].material.name == "PETG"


async def test_print_existing_file(setup):
    sim, p, client = setup
    assert (await client.post("/api/v1/files/usb/none.gcode", headers=AUTH)).status == 404
    assert (await client.post("/api/v1/files/sd/a.gcode", headers=AUTH)).status == 404
    await client.put("/api/v1/files/usb/a.gcode", data="M117 a\n", headers=AUTH)
    assert (await client.post("/api/v1/files/usb/a.gcode", headers=AUTH)).status == 204
    assert (await client.post("/api/v1/files/usb/a.gcode", headers=AUTH)).status == 409
    sim.tick(10)
    assert p.message == "a"


async def test_no_counters_shape_over_http():
    sim, p = make("2.1.0", [SimTool(0)])
    p.set_firmware("no_counters")
    async with TestClient(TestServer(prusalink.make_app(sim, "p"))) as client:
        data = await (await client.get("/api/v1/filament", headers=AUTH)).json()
    assert data == {"tools": [{"index": 0}]}


async def test_before_request_sees_each_request_and_may_change_the_printer():
    sim, p = make()
    seen = []

    def hook(method: str, path: str) -> None:
        seen.append((method, path))
        if path == "/api/v1/filament":
            p.set_firmware("no_endpoint")  # takes effect for this very request

    async with TestClient(TestServer(prusalink.make_app(sim, "p", hook))) as client:
        assert (await client.get("/api/v1/status", headers=AUTH)).status == 200
        assert (await client.get("/api/v1/filament", headers=AUTH)).status == 404
    assert seen == [("GET", "/api/v1/status"), ("GET", "/api/v1/filament")]


async def test_before_request_may_answer_or_drop_a_request():
    sim, p = make()

    def hook(method: str, path: str) -> prusalink.Reply:
        if path == "/api/v1/job":
            return prusalink.Answer(204)
        if path == "/api/version":
            return prusalink.DROP
        return None

    async with TestClient(TestServer(prusalink.make_app(sim, "p", hook))) as client:
        p.start_print(PrintSpec([Segment(0, 100)], duration_s=10))
        assert p.job_json() is not None
        assert (await client.get("/api/v1/status", headers=AUTH)).status == 200
        # The printer has a job, but the hook's answer is the one sent, and logged.
        assert (await client.get("/api/v1/job", headers=AUTH)).status == 204
        with pytest.raises(aiohttp.ClientConnectionError):
            await client.get("/api/version", headers=AUTH)
    requests = [e.text for e in sim.events if e.kind == "request"]
    assert requests == ["GET /api/v1/status 200", "GET /api/v1/job 204"]


async def test_off_printer_drops_the_connection(setup):
    sim, p, client = setup
    p.set_power("off")
    with pytest.raises(aiohttp.ClientConnectionError):
        await client.get("/api/v1/status", headers=AUTH)
    p.set_power("on")
    assert (await client.get("/api/v1/status", headers=AUTH)).status == 200


async def test_hung_printer_drops_the_request_once_it_comes_back(setup, monkeypatch):
    sim, p, client = setup
    real_sleep = asyncio.sleep

    async def quick(_seconds):
        await real_sleep(0.01)

    monkeypatch.setattr(prusalink.asyncio, "sleep", quick)
    p.set_power("hung")
    request = asyncio.ensure_future(client.get("/api/v1/status", headers=AUTH))
    await real_sleep(0.1)
    assert not request.done()  # still waiting, as the printer isn't answering
    p.set_power("on")
    with pytest.raises(aiohttp.ClientConnectionError):
        await asyncio.wait_for(request, 2)
    assert (await client.get("/api/v1/status", headers=AUTH)).status == 200


def test_serve_answers_without_a_socket():
    sim, p = make()
    answer = prusalink.serve(sim, "p", "GET", "/api/v1/status", {"x-API-key": KEY}, "")
    assert answer.status == 200 and answer.json == p.status_json()
    assert prusalink.serve(sim, "p", "GET", "/api/v1/job", AUTH, "") == prusalink.Answer(204)
    assert prusalink.serve(sim, "p", "GET", "/api/v1/status", {}, "").status == 401
    p.set_power("off")
    assert prusalink.serve(sim, "p", "GET", "/api/v1/status", AUTH, "") == prusalink.DROP
    requests = [e.text for e in sim.events if e.kind == "request"]
    assert requests == ["GET /api/v1/status 200", "GET /api/v1/job 204", "GET /api/v1/status 401"]


async def test_transport_serves_the_printer_in_process():
    sim, p = make()
    transport = prusalink.make_transport(sim, "p")
    async with httpx.AsyncClient(transport=transport, base_url="http://p", headers=AUTH) as client:
        assert (await client.get("/api/version")).json()["printer"] == "8.1.0"
        resp = await client.get("/api/v1/job")
        assert resp.status_code == 204 and resp.content == b""
        resp = await client.put("/api/v1/files/usb/s.gcode", content=JOB, headers=UPLOAD)
        assert resp.status_code == 201 and resp.json()["size"] == len(JOB)
        assert (await client.get("/api/v1/status")).json()["printer"]["state"] == "PRINTING"
        sim.tick(10)
        assert p.tools[0].material.name == "PETG"
        assert (await client.get("/api/v1/status", headers={"X-Api-Key": "no"})).status_code == 401
    requests = [e for e in sim.events if e.kind == "request"]
    assert requests[2].text == "PUT /api/v1/files/usb/s.gcode 201"
    assert requests[2].data == {"body": JOB}


async def test_transport_raises_when_the_printer_gives_no_answer():
    sim, p = make()

    def hook(method: str, path: str) -> prusalink.Reply:
        if path == "/api/v1/job":
            return prusalink.Answer(503, {"message": "busy"})
        return prusalink.DROP if path == "/api/version" else None

    transport = prusalink.make_transport(sim, "p", hook)
    async with httpx.AsyncClient(transport=transport, base_url="http://p", headers=AUTH) as client:
        resp = await client.get("/api/v1/job")
        assert resp.status_code == 503 and resp.json() == {"message": "busy"}
        with pytest.raises(httpx.RemoteProtocolError):
            await client.get("/api/version")
        p.set_power("hung")
        with pytest.raises(httpx.ReadTimeout):
            await client.get("/api/v1/status")
        p.set_power("off")
        with pytest.raises(httpx.ConnectError):
            await client.get("/api/v1/status")
        p.set_power("on")
        assert (await client.get("/api/v1/status")).status_code == 200
