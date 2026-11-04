import json
from dataclasses import replace

import httpx
import pytest

from spoolman_prusalink.app import Bridge
from spoolman_prusalink.config import Config, ConfigError, load
from spoolman_prusalink.prusalink import PrusaLink, parse_filament
from spoolman_prusalink.spoolman import Spoolman, parse_spool
from spoolman_prusalink.state import load_state

FILAMENT = {
    "boot_id": 3308742886,
    "tools": [{
        "index": 0,
        "extruded": 4310,
        "material": {"name": "PLA", "color": "#73737b",
                     "instance_uuid": "44c406a5-c6f5-4440-bfba-e8060564b717"},
    }],
}


def spool_json(id, location, material="PLA", color="73767d", uuid=None):
    extra = {} if uuid is None else {"instance_uuid": json.dumps(uuid)}
    return {
        "id": id, "location": location, "extra": extra,
        "filament": {"id": 1, "name": "Galaxy Black", "material": material,
                     "color_hex": color, "diameter": 1.75, "density": 1.24,
                     "vendor": {"name": "Prusament"}},
    }


def test_parse_filament_with_and_without_material():
    r = parse_filament({
        "boot_id": 5,
        "tools": [FILAMENT["tools"][0], {"index": 1, "extruded": 0}],
    })
    assert r.boot_id == 5
    assert r.tools[0].material.instance_uuid.startswith("44c4")
    assert r.tools[0].unload_count is None
    assert r.tools[1].material is None


def test_parse_filament_unload_fields():
    tool = {"index": 0, "extruded": 900, "unload_count": 2, "extruded_at_unload": 600,
            "material": {"name": "PLA"}}
    r = parse_filament({"boot_id": 5, "tools": [tool]}).tools[0]
    assert (r.extruded, r.unload_count, r.extruded_at_unload) == (900, 2, 600)


def test_parse_spool_decodes_uuid_extra():
    s = parse_spool(spool_json(3, "Core One L+", uuid="abc"), "instance_uuid")
    assert s.instance_uuid == "abc"
    assert s.name == "Prusament Galaxy Black"
    assert parse_spool(spool_json(3, ""), "instance_uuid").location is None


def make_config(tmp_path, **kw):
    return Config(
        printer_url="http://printer", printer_api_key="k", spoolman_url="http://spoolman",
        tools={0: "Core One L+"}, state_file=tmp_path / "state.json",
        ledger_file=tmp_path / "ledger.jsonl", **kw,
    )


def make_bridge(tmp_path, filament, spools):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/filament":
            assert request.headers["X-Api-Key"] == "k"
            return httpx.Response(200, json=filament())
        if request.url.path == "/api/v1/spool":
            return httpx.Response(200, json=spools())
        if request.url.path == "/api/v1/status":
            return httpx.Response(200, json={"printer": {"state": "PRINTING"}, "job": {"id": 9}})
        assert request.method == "GET", f"phase 1 must not write: {request.method} {request.url}"
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    cfg = make_config(tmp_path)
    return Bridge(
        config=cfg,
        printer=PrusaLink(cfg.printer_url, "k", transport=transport),
        spoolman=Spoolman(cfg.spoolman_url, uuid_field="instance_uuid", transport=transport),
    )


async def test_poll_logs_would_charge_and_persists(tmp_path, caplog):
    extruded = {"v": 4310}

    def filament():
        data = json.loads(json.dumps(FILAMENT))
        data["tools"][0]["extruded"] = extruded["v"]
        return data

    # A partial-match location must not be treated as the printer's location.
    spools = lambda: [spool_json(7, "Core One L+"), spool_json(8, "Core One L+ shelf")]  # noqa: E731
    bridge = make_bridge(tmp_path, filament, spools)
    caplog.set_level("INFO")

    await bridge.poll()  # baseline
    extruded["v"] = 5310
    await bridge.poll()
    assert "[Core One L+] WOULD CHARGE spool 7" in caplog.text
    assert "≈ +2.98 g" in caplog.text

    ledger = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    charges = [r for r in ledger if "use_length" in r]
    assert [(r["spool_id"], r["use_length"]) for r in charges] == [(7, 1000)]
    assert load_state(tmp_path / "state.json")[0].charged == 5310
    assert bridge.interval() == bridge.config.poll_printing
    await bridge.aclose()


async def test_two_spools_in_location_skips_tool(tmp_path, caplog):
    bridge = make_bridge(
        tmp_path, lambda: FILAMENT,
        lambda: [spool_json(7, "Core One L+"), spool_json(9, "Core One L+")],
    )
    await bridge.poll()
    assert "location holds spools 7, 9" in caplog.text
    assert 0 not in bridge.tools
    await bridge.aclose()


async def test_drift_logged_once(tmp_path, caplog):
    bridge = make_bridge(
        tmp_path, lambda: FILAMENT, lambda: [spool_json(7, "Core One L+", material="PETG")]
    )
    await bridge.poll()
    await bridge.poll()
    assert caplog.text.count("printer differs from spool 7") == 1
    await bridge.aclose()


async def test_printer_error_skips_poll(tmp_path, caplog):
    def handler(request):
        return httpx.Response(401)

    cfg = make_config(tmp_path)
    t = httpx.MockTransport(handler)
    bridge = Bridge(cfg, PrusaLink("http://p", "k", transport=t), Spoolman("http://s", transport=t))
    await bridge.poll()
    assert "printer read failed" in caplog.text
    assert not (tmp_path / "state.json").exists()
    await bridge.aclose()


def test_config_load(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[printer]\nurl = "http://192.168.1.22"\n'
        '[spoolman]\nurl = "http://localhost:7912"\n'
        '[[tools]]\nindex = 0\nlocation = "Core One L+"\n'
    )
    cfg = load(p, env={"PRUSALINK_API_KEY": "secret"})
    assert cfg.tools == {0: "Core One L+"}
    assert cfg.printer_api_key == "secret"
    assert cfg.dry_run

    assert cfg.length_factor == 1.0

    with pytest.raises(ConfigError, match="PRUSALINK_API_KEY"):
        load(p, env={})

    p.write_text(p.read_text() + "[bridge]\ndry_run = false\n")
    with pytest.raises(ConfigError, match="phase 1"):
        load(p, env={"PRUSALINK_API_KEY": "secret"})


async def test_length_factor_scales_charges(tmp_path):
    extruded = {"v": 1000}

    def filament():
        data = json.loads(json.dumps(FILAMENT))
        data["tools"][0]["extruded"] = extruded["v"]
        return data

    bridge = make_bridge(tmp_path, filament, lambda: [spool_json(7, "Core One L+")])
    bridge.config = replace(bridge.config, length_factor=1.05)
    await bridge.poll()
    extruded["v"] = 2000
    await bridge.poll()
    ledger = [json.loads(x) for x in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    charge = next(r for r in ledger if "use_length" in r)
    assert (charge["extruded_mm"], charge["use_length"]) == (1000, 1050.0)
    await bridge.aclose()
