"""The control API the page and scripts drive."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from simulator import control
from simulator.model import State
from simulator.tests.util import make


@pytest.fixture
async def setup(tmp_path):
    sim, printer = make()
    sim.data_dir = tmp_path
    client = TestClient(TestServer(control.make_app(sim)))
    await client.start_server()
    yield sim, printer, client
    await client.close()


async def test_state_and_page(setup):
    sim, p, client = setup
    data = await (await client.get("/api/state")).json()
    assert data["printers"][0]["tools"][0]["material"]["name"] == "PLA"
    assert data["printers"][0]["model"]["name"] == "COREONEL"
    assert (await client.get("/")).status == 200
    assert (await client.get("/static/../model.py")).status == 404


async def test_actions_and_errors(setup):
    sim, p, client = setup
    resp = await client.post("/api/printers/p/unload", json={"tool": 0})
    assert resp.status == 200 and p.state is State.BUSY
    resp = await client.post("/api/printers/p/unload", json={"tool": 0})
    assert resp.status == 409 and "error" in await resp.json()
    resp = await client.post("/api/printers/p/load", json={"tool": 0, "source": "nope"})
    assert resp.status == 400
    resp = await client.post("/api/printers/p/load", json={"tool": 0, "bogus": 1})
    assert resp.status == 400
    assert (await client.post("/api/printers/p/explode", json={})).status == 400
    assert (await client.post("/api/printers/q/unload", json={"tool": 0})).status == 404
    resp = await client.post("/api/printers/p/set_power", data="{bad")
    assert resp.status == 400
    sim.tick(5)
    body = {"segments": [{"tool": 0, "slicer_mm": 10}], "preheat_s": 0, "name": "x.bgcode"}
    load = {"tool": 0, "source": "preset", "name": "PLA"}
    resp = await client.post("/api/printers/p/load", json=load)
    assert resp.status == 200
    sim.tick(20)
    assert (await client.post("/api/printers/p/start_print", json=body)).status == 200
    assert p.job.name == "x.bgcode"
    assert (await client.post("/api/printers/p/start_print", json=body)).status == 409
    assert (await client.post("/api/printers/p/set_power", json={"power": "off"})).status == 200


async def test_settings_and_snapshots(setup):
    sim, p, client = setup
    assert (await client.post("/api/settings", json={"time_scale": 10})).status == 200
    assert sim.settings.time_scale == 10
    assert (await client.post("/api/settings", json={"time_scale": 0})).status == 400
    assert (await client.post("/api/settings", json={"nope": 1})).status == 400
    assert (await client.post("/api/settings", json={"start_blip": 1})).status == 400

    assert (await client.post("/api/snapshot/one")).status == 200
    assert await (await client.get("/api/snapshots")).json() == ["one"]
    p.tools[0].extruded = 999
    assert (await client.post("/api/restore/one")).status == 200
    assert sim.printers["p"].tools[0].extruded == 0
    assert (await client.post("/api/restore/two")).status == 404
    assert (await client.post("/api/snapshot/bad name")).status == 400


async def test_snapshot_listing_and_file_errors(setup):
    sim, p, client = setup
    (sim.data_dir / "bad name.json").write_text("{}")
    (sim.data_dir / "good.json").write_text("{}")
    assert await (await client.get("/api/snapshots")).json() == ["good"]
    assert (await client.post("/api/restore/good")).status == 400
    sim.data_dir = sim.data_dir / "good.json"  # a file, so writing under it fails
    resp = await client.post("/api/snapshot/one")
    assert resp.status == 400 and "error" in await resp.json()


async def test_close_ends_the_stream_wherever_it_is_in_a_batch(setup):
    sim, p, client = setup
    resp = await client.get("/api/events")
    await resp.content.readuntil(b"\n\n")  # the first state
    sim.changed()
    sim.push(("close", None))
    await asyncio.wait_for(resp.content.read(), 2)  # returns at EOF, not the 15 s heartbeat
    assert resp.content.at_eof()
