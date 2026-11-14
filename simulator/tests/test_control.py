"""The control API the page and scripts drive."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from simulator import control
from simulator.model import Fault, PrintSpec, Segment, State
from simulator.tests.util import make


@pytest.fixture
async def setup(tmp_path):
    sim, printer = make()
    sim.data_dir = tmp_path
    app = control.make_app(sim)
    client = TestClient(TestServer(app))
    await client.start_server()
    client.stream = app[control.STREAM]
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
    sim.tick(40)
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
    # JSON null unsets an override, so the model's value applies again.
    assert (await client.post("/api/settings", json={"purge_mm": 5})).status == 200
    assert sim.settings.purge_mm == 5
    assert (await client.post("/api/settings", json={"purge_mm": None})).status == 200
    assert sim.settings.purge_mm is None
    assert (await client.post("/api/settings", json={"time_scale": None})).status == 400
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
    client.stream.changed()
    client.stream.push(("close", None))
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
    assert resp.status == 400 and (await resp.json())["error"].startswith(
        "start_print needs segments"
    )
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


async def test_a_type_error_inside_an_action_is_a_bug_not_a_bad_request(setup, monkeypatch):
    sim, p, client = setup

    def broken() -> None:
        raise TypeError("a bug")

    monkeypatch.setattr(p, "home", broken)
    resp = await client.post("/api/printers/p/home")
    assert resp.status == 500


def test_the_readme_lists_every_action():
    readme = (Path(control.__file__).parent / "README.md").read_text()
    listed = re.search(r"^Actions: (.*?)\. Parameter names", readme, re.S | re.M).group(1)
    assert set(re.findall(r"`(\w+)`", listed)) == control.ACTIONS


def test_the_stream_queues_events_and_tick_changes():
    sim, p = make()
    stream = control.Stream(sim)
    queue = stream.subscribe()
    p.unload(0)
    items = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [kind for kind, _ in items] == ["log", "state", "log", "state"]
    sim.tick(0.1)  # the unload runs on: a state item, no log
    assert queue.get_nowait() == ("state", None) and queue.empty()
    stream.unsubscribe(queue)
    sim.tick(0.1)
    assert queue.empty()


def test_a_stream_queue_drops_the_oldest():
    sim, p = make()
    stream = control.Stream(sim)
    queue = stream.subscribe()
    for i in range(1500):
        stream.push(("n", i))
    assert queue.qsize() == 1000 and queue.get_nowait() == ("n", 500)


@pytest.mark.parametrize(
    ("action", "body", "error"),
    [
        (
            "set_sensor_fault",
            {"tool": 0, "value": "x"},
            "set_sensor_fault value must be true or false, got 'x'",
        ),
        ("set_auto_accept", {"value": 1}, "set_auto_accept value must be true or false, got 1"),
        ("unload", {"tool": True}, "unload tool must be an integer, got True"),
        ("unload", {"tool": 0.0}, "unload tool must be an integer, got 0.0"),
        (
            "set_extruded_at_unload",
            {"tool": 0, "value": 1.5},
            "set_extruded_at_unload value must be an integer, got 1.5",
        ),
        (
            "set_extruded_at_unload",
            {"tool": 0, "value": "3"},
            "set_extruded_at_unload value must be an integer, got '3'",
        ),
        ("extrude", {"tool": 0, "mm": "5"}, "extrude mm must be a finite number, got '5'"),
        ("extrude", {"tool": 0, "mm": False}, "extrude mm must be a finite number, got False"),
        ("load", {"tool": 0, "source": "preset", "name": 7}, "load name must be a string, got 7"),
        (
            "load",
            {"tool": 0, "temperatures": [215, "170", 60]},
            "load temperatures[1] must be an integer, got '170'",
        ),
        ("load", {"tool": 0, "temperatures": 215}, "load temperatures must be a list, got 215"),
        (
            "set_firmware",
            {"firmware": "bogus"},
            "set_firmware firmware must be one of full, no_counters, no_endpoint, got 'bogus'",
        ),
        (
            "start_print",
            {},
            'start_print needs segments, a list of {"tool": T, "slicer_mm": MM}, got None',
        ),
        ("start_print", {"segments": [{"tool": 0}]}, "start_print needs segments"),
        (
            "start_print",
            {"segments": [{"tool": "x", "slicer_mm": 1}]},
            "start_print segments[0] tool must be an integer, got 'x'",
        ),
        (
            "start_print",
            {"segments": [{"tool": 0, "slicer_mm": 1}], "preheat_s": "9"},
            "start_print preheat_s must be a finite number, got '9'",
        ),
        (
            "start_print",
            {"segments": [], "name": False},
            "start_print name must be a string, got False",
        ),
        (
            "start_print",
            {"segments": [], "color_changes_mm": 0},
            "start_print color_changes_mm must be a list, got 0",
        ),
        (
            "start_print",
            {"segments": [], "speed": 2},
            "start_print takes color_changes_mm, duration_s, name, preheat_s, segments, not speed",
        ),
    ],
)
async def test_parameters_are_checked_against_the_action_types(setup, action, body, error):
    sim, p, client = setup
    before = sim.to_json(events=False)
    resp = await client.post(f"/api/printers/p/{action}", json=body)
    assert resp.status == 400 and (await resp.json())["error"].startswith(error)
    assert sim.to_json(events=False) == before


async def test_checked_parameters_reach_the_action_as_their_types(setup):
    sim, p, client = setup
    body = {"segments": [{"tool": 0, "slicer_mm": 5}], "duration_s": 9, "color_changes_mm": [2]}
    assert (await client.post("/api/printers/p/start_print", json=body)).status == 200
    assert p.job.spec == PrintSpec([Segment(0, 5.0)], duration_s=9.0, color_changes_mm=[2.0])
    assert p.job.spec.name == "part.bgcode"
    assert (await client.post("/api/printers/p/set_fault", json={"fault": "error"})).status == 200
    assert p.fault is Fault.ERROR
    assert (
        await client.post("/api/printers/p/set_auto_accept", json={"value": None})
    ).status == 200
    assert p.auto_accept_model_check is None


def test_an_unknown_hint_is_a_bug():
    with pytest.raises(TypeError, match="no input check"):
        control._check("x", 1, dict)


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
async def test_a_number_that_isnt_finite_is_refused(setup, raw):
    sim, p, client = setup
    resp = await client.post(
        "/api/printers/p/extrude",
        data='{"tool": 0, "mm": ' + raw + "}",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400
    assert (await resp.json())["error"].startswith("extrude mm must be a finite number")


async def test_a_huge_integer_is_an_integer_but_not_a_float(setup):
    sim, p, client = setup
    huge = "1" + "0" * 400
    headers = {"Content-Type": "application/json"}
    data = '{"tool": ' + huge + "}"
    resp = await client.post("/api/printers/p/unload", data=data, headers=headers)
    assert resp.status == 400 and (await resp.json())["error"] == f"p has no tool {huge}"
    data = '{"tool": 0, "mm": ' + huge + "}"
    resp = await client.post("/api/printers/p/extrude", data=data, headers=headers)
    assert resp.status == 400
    assert (await resp.json())["error"].startswith("extrude mm must be a finite number")
