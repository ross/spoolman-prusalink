"""The control API the page and scripts drive."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

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
    huge = '{"purge_mm": 1' + "0" * 5000 + "}"
    resp = await client.post("/api/settings", data=huge)
    assert resp.status == 400 and "bad JSON" in (await resp.json())["error"]

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
    (sim.data_dir / "huge.json").write_text('{"now": 1' + "0" * 5000 + "}")
    assert (await client.post("/api/restore/huge")).status == 400
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


async def test_advance_moves_simulated_time(setup):
    sim, p, client = setup
    p.unload(0)  # three simulated seconds by default
    assert p.state is State.BUSY
    resp = await client.post("/api/advance", json={"seconds": 5})
    assert resp.status == 200 and (await resp.json())["now"] == pytest.approx(5)
    assert p.state is State.IDLE and p.tools[0].unload_count == 1
    assert (await client.post("/api/advance", json={"seconds": 0})).status == 400
    assert (await client.post("/api/advance", json={"seconds": "x"})).status == 400


async def test_static_files_are_served_and_the_root_is_not_escaped(setup):
    sim, p, client = setup
    resp = await client.get("/static/app.js")
    assert resp.status == 200 and "javascript" in resp.content_type
    assert (await client.get("/static/nothing.js")).status == 404
    # A path that climbs out of the folder, sent without the client tidying it away.
    for path in ("/static/%2e%2e/model.py", "/static/..%2fmodel.py", "/static/%2e%2e%2fmodel.py"):
        resp = await client.get(URL(path, encoded=True))
        assert resp.status == 404, path
        assert "def " not in await resp.text()


async def test_the_page_before_it_is_built(setup, tmp_path, monkeypatch):
    sim, p, client = setup
    monkeypatch.setattr(control, "STATIC", tmp_path)
    resp = await client.get("/")
    assert resp.status == 200 and "isn't built yet" in await resp.text()


async def test_the_body_must_be_a_json_object(setup):
    sim, p, client = setup
    for body in ("[1]", '"x"', "3"):
        resp = await client.post("/api/printers/p/unload", data=body)
        assert resp.status == 400
        assert (await resp.json())["error"] == "the body must be a JSON object"
    assert p.state is State.IDLE


async def test_an_action_the_printer_has_no_method_for_is_refused(setup):
    sim, p, client = setup
    resp = await client.post("/api/printers/p/start_print", json={"segments": "x"})
    assert resp.status == 400 and (await resp.json())["error"].startswith("bad print")
    resp = await client.post("/api/printers/p/unload", json={})
    assert resp.status == 400 and "bad parameters for unload" in (await resp.json())["error"]
    resp = await client.post("/api/printers/p/_log", json={})
    assert resp.status == 400 and "unknown action" in (await resp.json())["error"]


async def test_snapshots_need_a_data_dir(setup):
    sim, p, client = setup
    sim.data_dir = None
    resp = await client.post("/api/snapshot/one")
    assert resp.status == 400 and (await resp.json())["error"] == "no data_dir for snapshots"
    resp = await client.post("/api/restore/one")
    assert resp.status == 400
    assert await (await client.get("/api/snapshots")).json() == []


async def test_an_action_without_a_body_takes_no_parameters(setup):
    sim, p, client = setup
    resp = await client.post("/api/printers/p/home")
    assert resp.status == 200 and p.homed
    resp = await client.post("/api/printers/p/dismiss")
    assert resp.status == 409 and "nothing to dismiss" in (await resp.json())["error"]
