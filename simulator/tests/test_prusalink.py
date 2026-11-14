"""The PrusaLink clone over HTTP, as the bridge calls it."""

from __future__ import annotations

import asyncio

import aiohttp
import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from simulator import prusalink
from simulator.model import Firmware, PrintSpec, Segment, SimTool, State
from simulator.tests.test_model import PETG, shown, tool0
from simulator.tests.util import KEY, PLA, loaded, make

AUTH = {"X-Api-Key": KEY}
UPLOAD = {**AUTH, "Print-After-Upload": "?1", "Overwrite": "?1", "Content-Type": "text/x.gcode"}
PREAMBLE = 'M862.3 P "COREONEL"\nM862.5 P2\nM862.6 P"Input shaper"\n'
JOB = PREAMBLE + 'M865 X R N"PETG" O"#186bb5" L0\nM117 Spoolman: test\n'


@pytest.fixture
async def setup():
    sim, printer = make()
    client = TestClient(TestServer(prusalink.make_app(sim, "p")))
    await client.start_server()
    yield sim, printer, client
    await client.close()


async def test_info_and_version_follow_the_firmware(setup):
    sim, p, client = setup
    for firmware in Firmware:
        p.set_firmware(firmware)
        resp = await client.get("/api/v1/info", headers=AUTH)
        assert resp.status == 200
        info = await resp.json()
        assert list(info) == [
            "nozzle_diameter",
            "mmu",
            "serial",
            "hostname",
            "min_extrusion_temp",
        ]
    assert info == {
        "nozzle_diameter": 0.4,
        "mmu": False,
        "serial": "SIM-p",
        "hostname": "p",
        "min_extrusion_temp": 170,
    }
    assert (await client.get("/api/v1/info")).status == 401
    assert (await client.post("/api/v1/info", headers=AUTH)).status == 405

    version = await (await client.get("/api/version", headers=AUTH)).json()
    assert list(version) == [
        "api",
        "server",
        "nozzle_diameter",
        "text",
        "hostname",
        "firmware",
        "printer",
        "capabilities",
    ]
    assert version["nozzle_diameter"] == info["nozzle_diameter"]


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
    assert requests[0].data == {"method": "GET", "path": "/api/v1/status", "status": 200}
    assert requests[1].data == {
        "method": "PUT",
        "path": "/api/v1/files/usb/a.gcode",
        "status": 201,
        "body": "M117 x\n",
    }


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
        "color": "#186bb5",
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
    body = PREAMBLE + "M117 x\n"
    assert (await client.put("/api/v1/files/usb/a.gcode", data=body, headers=headers)).status == 201
    sim.tick(100)
    resp = await client.put("/api/v1/files/usb/a.gcode", data=body, headers=headers)
    assert resp.status == 409
    assert (await resp.json())["message"] == "File already exists"

    p.unload(0)  # BUSY
    resp = await client.put("/api/v1/files/usb/b.gcode", data="y", headers=UPLOAD)
    assert resp.status == 409
    assert (await resp.json())["message"] == "Can't print right now"
    assert p.files["b.gcode"] == "y"


async def get(client, path):
    resp = await client.get(path, headers=AUTH)
    return resp.status, await resp.json() if resp.status == 200 else None


async def test_the_preview_over_http(setup):
    sim, p, client = setup
    body = JOB.replace("COREONEL", "MINI")
    resp = await client.put("/api/v1/files/usb/s.gcode", data=body, headers=UPLOAD)
    assert resp.status == 201

    # The scan: the job is counted and visible at once.
    _, status = await get(client, "/api/v1/status")
    assert status["printer"]["state"] == "PRINTING" and status["job"]["id"] == 1
    _, job = await get(client, "/api/v1/job")
    assert (job["id"], job["state"]) == (1, "PRINTING")

    # The question: ATTENTION, with the job still there.
    sim.tick(0.5)
    _, status = await get(client, "/api/v1/status")
    assert status["printer"]["state"] == "ATTENTION" and status["job"]["id"] == 1
    _, job = await get(client, "/api/v1/job")
    assert (job["id"], job["state"]) == (1, "PAUSED")

    # A second upload is refused, and its file kept.
    resp = await client.put("/api/v1/files/usb/b.gcode", data=JOB, headers=UPLOAD)
    assert resp.status == 409 and (await resp.json())["message"] == "Can't print right now"
    assert p.files["b.gcode"] == JOB

    # Print: the blip, IDLE with no job, then the job runs to FINISHED.
    p.tap_through()
    _, status = await get(client, "/api/v1/status")
    assert status["printer"]["state"] == "IDLE" and "job" not in status
    sim.tick(0.3)
    _, status = await get(client, "/api/v1/status")
    assert status["printer"]["state"] == "PRINTING" and status["job"]["id"] == 1
    sim.tick(10)
    assert p.state is State.FINISHED and p.tools[0].material.name == "PETG"

    # Abort: IDLE with no job, and the next job's ID is one higher.
    resp = await client.put("/api/v1/files/usb/s.gcode", data=body, headers=UPLOAD)
    assert resp.status == 201
    sim.tick(0.5)
    _, status = await get(client, "/api/v1/status")
    assert status["printer"]["state"] == "ATTENTION" and status["job"]["id"] == 2
    p.cancel()
    _, status = await get(client, "/api/v1/status")
    assert status["printer"]["state"] == "IDLE" and "job" not in status
    assert await get(client, "/api/v1/job") == (204, None)
    resp = await client.put("/api/v1/files/usb/b.gcode", data=JOB, headers=UPLOAD)
    assert resp.status == 201
    _, status = await get(client, "/api/v1/status")
    assert status["job"]["id"] == 3


async def test_print_existing_file(setup):
    sim, p, client = setup
    assert (await client.post("/api/v1/files/usb/none.gcode", headers=AUTH)).status == 404
    assert (await client.post("/api/v1/files/sd/a.gcode", headers=AUTH)).status == 404
    await client.put("/api/v1/files/usb/a.gcode", data=PREAMBLE + "M117 a\n", headers=AUTH)
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
        assert prusalink.job(p) is not None
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
    assert answer.status == 200 and answer.json == prusalink.status(p)
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
    assert requests[2].data["body"] == JOB


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


def test_filament_full():
    sim, p = make()
    tool0(p).extruded = 12.9
    assert prusalink.filament(p) == {
        "boot_id": p.boot_id,
        "tools": [
            {
                "index": 0,
                "extruded": 12,
                "unload_count": 0,
                "extruded_at_unload": 0,
                "material": {
                    "name": "PLA",
                    "nozzle_temperature": 215,
                    "preheat_temperature": 170,
                    "bed_temperature": 60,
                    "color": "#73737b",
                    "instance_uuid": PLA.instance_uuid,
                },
            }
        ],
    }


def test_filament_no_counters_and_no_endpoint():
    sim, p = make()
    p.set_firmware("no_counters")
    assert prusalink.filament(p) == {
        "tools": [
            {
                "index": 0,
                "material": {
                    "name": "PLA",
                    "nozzle_temperature": 215,
                    "preheat_temperature": 170,
                    "bed_temperature": 60,
                    "color": "#73737b",
                    "instance_uuid": PLA.instance_uuid,
                },
            }
        ]
    }
    p.set_firmware(Firmware.NO_ENDPOINT)
    assert prusalink.filament(p) is None


def test_filament_multi_tool_skips_disabled_and_hides_uuids():
    tools = [
        loaded(0, PLA),
        SimTool(1, enabled=False),
        loaded(2, PETG),
        SimTool(3),
    ]
    sim, p = make("8.10.0", tools)
    data = prusalink.filament(p)
    assert [t["index"] for t in data["tools"]] == [0, 2, 3]
    assert shown(p) == {"name": "PLA", "color": "#73737b"}
    assert "material" not in data["tools"][2]
    p.set_sensor_fault(2, True)
    assert "material" not in prusalink.filament(p)["tools"][1]
