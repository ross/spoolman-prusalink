import json
import re
from dataclasses import replace

import httpx
import pytest

from spoolman_prusalink.__main__ import main
from spoolman_prusalink.accounting import ToolState
from spoolman_prusalink.app import Bridge
from spoolman_prusalink.config import Config, ConfigError, load
from spoolman_prusalink.prusalink import PrusaLink, parse_filament
from spoolman_prusalink.spoolman import Spoolman, parse_spool
from spoolman_prusalink.state import State, load_state, save_state

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


def test_parse_spool_remaining_weight_is_not_clamped():
    data = spool_json(3, "Core One L+")
    data["filament"]["weight"] = 1000.0
    data["used_weight"], data["remaining_weight"] = 1012.0, 0.0  # Spoolman clamps at 0
    assert parse_spool(data, None).remaining_weight == -12.0
    data["initial_weight"] = 900.0  # the spool's own weight wins
    assert parse_spool(data, None).net_weight == 900.0
    data["initial_weight"] = 0.0  # a weight of 0 is still the spool's own, not missing
    assert parse_spool(data, None).net_weight == 0.0
    del data["used_weight"]
    assert parse_spool(data, None).remaining_weight is None


def test_parse_spool_decodes_uuid_extra():
    s = parse_spool(spool_json(3, "Core One L+", uuid="abc"), "instance_uuid")
    assert s.instance_uuid == "abc"
    assert s.name == "Prusament Galaxy Black"
    assert parse_spool(spool_json(3, ""), "instance_uuid").location is None


def test_parse_spool_claim_and_last_used():
    data = spool_json(3, "Core One L+")
    assert parse_spool(data, claim_field="bridge_claim").claim is False
    assert parse_spool(data).last_used is None
    data["extra"]["bridge_claim"] = "true"
    data["last_used"] = "2026-11-07T01:47:16Z"
    s = parse_spool(data, claim_field="bridge_claim")
    assert s.claim is True
    assert s.last_used.isoformat() == "2026-11-07T01:47:16+00:00"
    assert parse_spool(data).claim is False  # Claims off.
    data["extra"]["bridge_claim"] = "false"
    data["last_used"] = "2026-11-07T01:47:16"  # No zone: read as UTC.
    s = parse_spool(data, claim_field="bridge_claim")
    assert s.claim is False and s.last_used.utcoffset().total_seconds() == 0


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
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"printer": "8.1.0"})
        if request.url.path == "/api/v1/job":
            return httpx.Response(200, json={"id": 9, "file": {"display_name": "part.bgcode"}})
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


async def test_poll_logs_would_charge_and_saves_nothing(tmp_path, caplog):
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
    assert bridge.tools[0].charged == 5310
    assert not (tmp_path / "state.json").exists()  # Dry run saves nothing.
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


async def test_printer_outage_logged_on_change(tmp_path, caplog):
    up = {"v": False}
    now = {"v": 0.0}

    def handler(request):
        if not up["v"]:
            raise httpx.ConnectError("All connection attempts failed")
        return httpx.Response(401)

    cfg = make_config(tmp_path)
    t = httpx.MockTransport(handler)
    bridge = Bridge(cfg, PrusaLink("http://p", "k", transport=t), Spoolman("http://s", transport=t))
    bridge._printer_outage.clock = lambda: now["v"]
    caplog.set_level("INFO")

    for _ in range(5):
        await bridge.poll()
        now["v"] += 60
    assert caplog.text.count("printer unreachable") == 1
    assert "still unreachable" not in caplog.text
    assert bridge.interval() == cfg.poll_idle

    now["v"] = 3 * 3600 + 30
    await bridge.poll()
    assert "printer still unreachable after 3h 00m" in caplog.text

    up["v"] = True
    await bridge.poll()
    assert "printer reachable again after 3h 00m" in caplog.text
    assert not bridge._printer_outage.down
    await bridge.aclose()


def test_save_state_skips_unchanged(tmp_path):
    path = tmp_path / "state.json"
    state = State(tools={0: ToolState(boot_id=1, charged=100)})
    assert save_state(path, state)
    assert not save_state(path, state)
    state.tools[0] = replace(state.tools[0], charged=150)
    assert save_state(path, state)
    assert load_state(path).tools[0].charged == 150


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

    assert cfg.flag_field == "bridge_flag"
    assert cfg.claim_field == "bridge_claim"
    base = p.read_text()
    p.write_text(base + "[bridge]\nclaim_field = \"bridge_flag\"\n")
    with pytest.raises(ConfigError, match="must differ"):
        load(p, env={"PRUSALINK_API_KEY": "secret"})
    p.write_text(base + "[bridge]\nclaim_field = \"\"\n")
    assert load(p, env={"PRUSALINK_API_KEY": "secret"}).claim_field is None
    p.write_text(base)
    p.write_text(p.read_text() + "[bridge]\ndry_run = false\nflag_field = \"Bad Key\"\n")
    with pytest.raises(ConfigError, match="flag_field"):
        load(p, env={"PRUSALINK_API_KEY": "secret"})


CONFIG_BASE = (
    '[printer]\nurl = "http://p"\n[spoolman]\nurl = "http://s"\n'
    '[[tools]]\nindex = 0\nlocation = "Core One L+"\n'
)


@pytest.mark.parametrize("text, error", [
    ('[bridge]\nprinter_sync = "false"\n', "bridge.printer_sync must be true or false"),
    ('[bridge]\nflag_drift = "no"\n', "bridge.flag_drift must be true or false"),
    ('[bridge]\nsync_drift = 1\n', "bridge.sync_drift must be true or false"),
    ('[bridge]\nprinter_notices = "yes"\n', "bridge.printer_notices must be true or false"),
    ('[bridge]\ndry_run = "false"\n', "bridge.dry_run must be true or false"),
    ('[printer]\nuuids = "false"\n', "printer.uuids must be true or false"),
    ('[printer]\ngcode_preamble = "M862.3"\n', "gcode_preamble must be a list of strings"),
    ('[printer]\ngcode_preamble = ["M862.3", 1]\n', "gcode_preamble must be a list of strings"),
    ('[bridge]\npoll_idle = 0\n', "bridge.poll_idle must be a number greater than 0"),
    ('[bridge]\npoll_printing = -5\n', "bridge.poll_printing must be a number greater than 0"),
    ('[bridge]\npoll_idle = "soon"\n', "bridge.poll_idle must be a number greater than 0"),
    ('[bridge]\nlength_factor = "x"\n', "bridge.length_factor must be a number"),
    ('[bridge]\npoll_idle = nan\n', "bridge.poll_idle must be a number greater than 0"),
    ('[bridge]\npoll_printing = inf\n', "bridge.poll_printing must be a number greater than 0"),
    ('[bridge]\npoll_printing = -nan\n', "bridge.poll_printing must be a number greater than 0"),
    ('[bridge]\nlength_factor = nan\n', "bridge.length_factor must be a number"),
    ('[bridge]\nlength_factor = inf\n', "bridge.length_factor must be a number"),
    ('[bridge]\nuuid_field = 5\n', "bridge.uuid_field must be a string"),
    ('[bridge]\nflag_field = true\n', "bridge.flag_field must be a string"),
    ('[bridge]\nclaim_field = ["a"]\n', "bridge.claim_field must be a string"),
    ('[bridge]\nstate_file = 5\n', "bridge.state_file must be a string"),
    ('[bridge]\nledger_file = 5\n', "bridge.ledger_file must be a string"),
    ('[bridge]\neviction_location = 5\n', "bridge.eviction_location must be a string"),
    ('[printer]\nsync_storage = 5\n', "printer.sync_storage must be a string"),
    ('[printer]\nsync_file = 5\n', "printer.sync_file must be a string"),
    ('[printer]\napi_key = 5\n', "printer.api_key must be a string"),
])
def test_config_rejects_bad_values(tmp_path, text, error):
    p = tmp_path / "c.toml"
    # TOML can't define [printer] twice, so its keys go into the base's table.
    p.write_text(CONFIG_BASE.replace("[printer]\n", text, 1) if text.startswith("[printer]")
                 else CONFIG_BASE + text)
    with pytest.raises(ConfigError, match=re.escape(error)):
        load(p, env={"PRUSALINK_API_KEY": "k"})


@pytest.mark.parametrize("text, error", [
    ("[printer\n", "can't parse"),
    ('[printer]\nurl = "http://p"\n[spoolman]\nurl = "http://s"\n[[tools]]\nlocation = "A"\n',
     "each [[tools]] entry needs an index and a location"),
    ('[printer]\nurl = "http://p"\n[spoolman]\nurl = "http://s"\n[[tools]]\nindex = 0\n',
     "each [[tools]] entry needs an index and a location"),
    ('[printer]\nurl = "http://p"\n[spoolman]\nurl = "http://s"\n'
     '[[tools]]\nindex = "zero"\nlocation = "A"\n', "tool index must be an integer"),
])
def test_config_malformed_file_is_a_config_error(tmp_path, text, error):
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match=re.escape(error)):
        load(p, env={"PRUSALINK_API_KEY": "k"})


@pytest.mark.parametrize("text, error", [
    ('[printer]\nurl = 5\n[spoolman]\nurl = "http://s"\n', "printer.url must be a string"),
    ('[printer]\nurl = "http://p"\n[spoolman]\nurl = 5\n', "spoolman.url must be a string"),
    ('[printer]\nurl = "http://p"\n[spoolman]\nurl = "http://s"\n'
     '[[tools]]\nindex = 0\nlocation = 5\n', "tool location must be a string"),
])
def test_config_rejects_non_string_urls_and_locations(tmp_path, text, error):
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match=re.escape(error)):
        load(p, env={"PRUSALINK_API_KEY": "k"})


def test_config_file_that_is_not_utf8_is_a_config_error(tmp_path):
    p = tmp_path / "c.toml"
    p.write_bytes(b'[printer]\nurl = "\xff\xfe"\n')
    with pytest.raises(ConfigError, match="can't parse"):
        load(p, env={"PRUSALINK_API_KEY": "k"})


def test_main_exits_2_on_a_malformed_config(tmp_path, monkeypatch, caplog):
    p = tmp_path / "c.toml"
    p.write_text("[printer\n")
    monkeypatch.setenv("PRUSALINK_API_KEY", "k")
    assert main(["-c", str(p), "once"]) == 2
    assert "config: can't parse" in caplog.text


def test_config_accepts_booleans_and_preamble_list(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(CONFIG_BASE.replace(
        '[printer]\n', '[printer]\nuuids = false\ngcode_preamble = ["M862.3 P \\"COREONEL\\""]\n')
        + "[bridge]\nprinter_sync = false\ndry_run = false\npoll_idle = 0.5\n")
    cfg = load(p, env={"PRUSALINK_API_KEY": "k"})
    assert cfg.printer_sync is False and cfg.dry_run is False and cfg.printer_uuids is False
    assert cfg.gcode_preamble == ('M862.3 P "COREONEL"',)
    assert cfg.poll_idle == 0.5


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


def test_config_from_environment_only():
    cfg = load(None, env={
        "PRUSALINK_URL": "http://p", "PRUSALINK_API_KEY": "k", "SPOOLMAN_URL": "http://s",
        "SPOOLMAN_PRUSALINK_LOCATION": "Core One L+", "SPOOLMAN_PRUSALINK_DRY_RUN": "false",
    })
    assert cfg.tools == {0: "Core One L+"} and cfg.dry_run is False
    assert cfg.state_file.as_posix() == "/data/state.json"
    with pytest.raises(ConfigError, match="SPOOLMAN_PRUSALINK_LOCATION"):
        load(None, env={"PRUSALINK_URL": "p", "PRUSALINK_API_KEY": "k", "SPOOLMAN_URL": "s"})
    with pytest.raises(ConfigError, match="true or false"):
        load(None, env={"PRUSALINK_URL": "p", "PRUSALINK_API_KEY": "k", "SPOOLMAN_URL": "s",
                        "SPOOLMAN_PRUSALINK_LOCATION": "x", "SPOOLMAN_PRUSALINK_DRY_RUN": "maybe"})
