import json
import re
from dataclasses import replace

import httpx
import pytest

from spoolman_prusalink import printer_sync
from spoolman_prusalink.__main__ import main
from spoolman_prusalink.accounting import Provisional, SwapFlag, ToolState
from spoolman_prusalink.app import Bridge
from spoolman_prusalink.config import Config, ConfigError, PrinterConfig, load
from spoolman_prusalink.models import Material
from spoolman_prusalink.prusalink import PrusaLink, parse_filament
from spoolman_prusalink.spoolman import Spoolman, parse_spool
from spoolman_prusalink.state import PrinterState, State, load_state, save_state

FILAMENT = {
    "boot_id": 3308742886,
    "tools": [
        {
            "index": 0,
            "extruded": 4310,
            "material": {
                "name": "PLA",
                "color": "#73737b",
                "instance_uuid": "44c406a5-c6f5-4440-bfba-e8060564b717",
                "nozzle_temperature": 215,
                "preheat_temperature": 170,
                "bed_temperature": 60,
            },
        }
    ],
}


def spool_json(id, location, material="PLA", color="73767d", uuid=None):
    extra = {} if uuid is None else {"instance_uuid": json.dumps(uuid)}
    return {
        "id": id,
        "location": location,
        "extra": extra,
        "filament": {
            "id": 1,
            "name": "Galaxy Black",
            "material": material,
            "color_hex": color,
            "diameter": 1.75,
            "density": 1.24,
            "vendor": {"name": "Prusament"},
        },
    }


def test_parse_filament_reads_tag_uuids():
    tool = json.loads(json.dumps(FILAMENT["tools"][0]))
    r = parse_filament({"boot_id": 5, "tools": [tool]})
    assert r.tools[0].material.from_tag is False
    tool["material"]["material_uuid"] = "0b1c6b8e-8d0f-4f8e-9a63-2b1f0d6c7e11"
    tool["material"]["package_uuid"] = "5d6e7f80-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
    m = parse_filament({"boot_id": 5, "tools": [tool]}).tools[0].material
    assert m.material_uuid.startswith("0b1c") and m.package_uuid.startswith("5d6e")
    assert m.from_tag is True


def test_parse_filament_with_and_without_material():
    r = parse_filament(
        {
            "boot_id": 5,
            "tools": [FILAMENT["tools"][0], {"index": 1, "extruded": 0}],
        }
    )
    assert r.boot_id == 5
    assert r.tools[0].material.instance_uuid.startswith("44c4")
    assert r.tools[0].unload_count is None
    assert r.tools[1].material is None


def test_parse_filament_unload_fields():
    tool = {
        "index": 0,
        "extruded": 900,
        "unload_count": 2,
        "extruded_at_unload": 600,
        "material": {
            "name": "PLA",
            "nozzle_temperature": 215,
            "preheat_temperature": 170,
            "bed_temperature": 60,
        },
    }
    r = parse_filament({"boot_id": 5, "tools": [tool]}).tools[0]
    assert (r.extruded, r.unload_count, r.extruded_at_unload) == (900, 2, 600)


def test_parse_filament_requires_temperatures():
    tool = {"index": 0, "extruded": 0, "material": {"name": "PLA"}}
    with pytest.raises(KeyError):
        parse_filament({"boot_id": 5, "tools": [tool]})
    r = parse_filament(FILAMENT).tools[0].material
    assert (r.nozzle_temperature, r.preheat_temperature, r.bed_temperature) == (215, 170, 60)


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
    printer = PrinterConfig(
        name="default", url="http://printer", api_key="k", tools={0: "Core One L+"}
    )
    return Config(
        spoolman_url="http://spoolman",
        printers=(printer,),
        state_file=tmp_path / "state.json",
        ledger_file=tmp_path / "ledger.jsonl",
        **kw,
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
        spoolman=Spoolman(cfg.spoolman_url, uuid_field="instance_uuid", transport=transport),
        clients={"default": PrusaLink(cfg.printers[0].url, "k", transport=transport)},
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
    assert any("event" in r for r in ledger)
    assert all(r["dry_run"] is True for r in ledger)  # Events too, not just charges.
    assert bridge.printers[0].tools[0].charged == 5310
    assert not (tmp_path / "state.json").exists()  # Dry run saves nothing.
    assert bridge.interval() == bridge.config.poll_printing
    await bridge.aclose()


async def test_two_spools_in_location_skips_tool(tmp_path, caplog):
    bridge = make_bridge(
        tmp_path,
        lambda: FILAMENT,
        lambda: [spool_json(7, "Core One L+"), spool_json(9, "Core One L+")],
    )
    await bridge.poll()
    assert "location holds spools 7, 9" in caplog.text
    assert 0 not in bridge.printers[0].tools
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
    bridge = Bridge(
        cfg, Spoolman("http://s", transport=t), {"default": PrusaLink("http://p", "k", transport=t)}
    )
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
    bridge = Bridge(
        cfg, Spoolman("http://s", transport=t), {"default": PrusaLink("http://p", "k", transport=t)}
    )
    bridge.printers[0]._printer_outage.clock = lambda: now["v"]
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
    assert not bridge.printers[0]._printer_outage.down
    await bridge.aclose()


def test_save_state_skips_unchanged(tmp_path):
    path = tmp_path / "state.json"
    state = State(printers={"default": PrinterState({0: ToolState(boot_id=1, charged=100)})})
    assert save_state(path, state)
    assert not save_state(path, state)
    tools = state.tools("default")
    tools[0] = replace(tools[0], charged=150)
    assert save_state(path, state)
    assert load_state(path, ["default"]).tools("default")[0].charged == 150


def test_config_load(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[printer]\nurl = "http://192.168.1.22"\n'
        '[spoolman]\nurl = "http://localhost:7912"\n'
        '[[tools]]\nindex = 0\nlocation = "Core One L+"\n'
    )
    cfg = load(p, env={"PRUSALINK_API_KEY": "secret"})
    assert cfg.printers[0].tools == {0: "Core One L+"}
    assert cfg.printers[0].api_key == "secret"
    assert cfg.printers[0].name == "default"
    assert cfg.dry_run

    assert cfg.length_factor == 1.0
    # P1 and C2: poll every 30 s while printing and every 60 s otherwise.
    assert cfg.poll_printing == 30
    assert cfg.poll_idle == 60
    assert cfg.sync_delay == 0

    with pytest.raises(ConfigError, match="PRUSALINK_API_KEY"):
        load(p, env={})

    assert cfg.flag_field == "bridge_flag"
    assert cfg.claim_field == "bridge_claim"
    base = p.read_text()
    p.write_text(base + '[bridge]\nclaim_field = "bridge_flag"\n')
    with pytest.raises(ConfigError, match="must differ"):
        load(p, env={"PRUSALINK_API_KEY": "secret"})
    p.write_text(base + '[bridge]\nclaim_field = ""\n')
    assert load(p, env={"PRUSALINK_API_KEY": "secret"}).claim_field is None
    p.write_text(base)
    p.write_text(p.read_text() + '[bridge]\ndry_run = false\nflag_field = "Bad Key"\n')
    with pytest.raises(ConfigError, match="flag_field"):
        load(p, env={"PRUSALINK_API_KEY": "secret"})


CONFIG_BASE = (
    '[printer]\nurl = "http://p"\n[spoolman]\nurl = "http://s"\n'
    '[[tools]]\nindex = 0\nlocation = "Core One L+"\n'
)


@pytest.mark.parametrize(
    "text, error",
    [
        ('[bridge]\nprinter_sync = "false"\n', "bridge.printer_sync must be true or false"),
        ('[bridge]\nflag_drift = "no"\n', "bridge.flag_drift must be true or false"),
        ("[bridge]\nsync_drift = 1\n", "bridge.sync_drift must be true or false"),
        ('[bridge]\ndry_run = "false"\n', "bridge.dry_run must be true or false"),
        ('[printer]\nuuids = "false"\n', "printer.uuids must be true or false"),
        ('[printer]\ngcode_preamble = "M862.3"\n', "gcode_preamble must be a list of strings"),
        ('[printer]\ngcode_preamble = ["M862.3", 1]\n', "gcode_preamble must be a list of strings"),
        ("[bridge]\npoll_idle = 0\n", "bridge.poll_idle must be a number greater than 0"),
        ("[bridge]\nsync_delay = -1\n", "bridge.sync_delay must be a number of seconds"),
        ('[bridge]\nsync_delay = "5m"\n', "bridge.sync_delay must be a number of seconds"),
        ("[bridge]\npoll_printing = -5\n", "bridge.poll_printing must be a number greater than 0"),
        ('[bridge]\npoll_idle = "soon"\n', "bridge.poll_idle must be a number greater than 0"),
        ('[bridge]\nlength_factor = "x"\n', "bridge.length_factor must be a number"),
        ("[bridge]\npoll_idle = nan\n", "bridge.poll_idle must be a number greater than 0"),
        ("[bridge]\npoll_printing = inf\n", "bridge.poll_printing must be a number greater than 0"),
        (
            "[bridge]\npoll_printing = -nan\n",
            "bridge.poll_printing must be a number greater than 0",
        ),
        ("[bridge]\nlength_factor = nan\n", "bridge.length_factor must be a number"),
        ("[bridge]\nlength_factor = inf\n", "bridge.length_factor must be a number"),
        ("[bridge]\nuuid_field = 5\n", "bridge.uuid_field must be a string"),
        ("[bridge]\nflag_field = true\n", "bridge.flag_field must be a string"),
        ('[bridge]\nclaim_field = ["a"]\n', "bridge.claim_field must be a string"),
        ("[bridge]\nstate_file = 5\n", "bridge.state_file must be a string"),
        ("[bridge]\nledger_file = 5\n", "bridge.ledger_file must be a string"),
        ("[bridge]\neviction_location = 5\n", "bridge.eviction_location must be a string"),
        ("[printer]\nsync_storage = 5\n", "printer.sync_storage must be a string"),
        ("[printer]\nsync_file = 5\n", "printer.sync_file must be a string"),
        ("[printer]\napi_key = 5\n", "printer.api_key must be a string"),
    ],
)
def test_config_rejects_bad_values(tmp_path, text, error):
    p = tmp_path / "c.toml"
    # TOML can't define [printer] twice, so its keys go into the base's table.
    p.write_text(
        CONFIG_BASE.replace("[printer]\n", text, 1)
        if text.startswith("[printer]")
        else CONFIG_BASE + text
    )
    with pytest.raises(ConfigError, match=re.escape(error)):
        load(p, env={"PRUSALINK_API_KEY": "k"})


@pytest.mark.parametrize(
    "text, error",
    [
        ("[printer\n", "can't parse"),
        (
            '[printer]\nurl = "http://p"\n[spoolman]\nurl = "http://s"\n'
            '[[tools]]\nlocation = "A"\n',
            "each [[tools]] entry needs an index and a location",
        ),
        (
            '[printer]\nurl = "http://p"\n[spoolman]\nurl = "http://s"\n[[tools]]\nindex = 0\n',
            "each [[tools]] entry needs an index and a location",
        ),
        (
            '[printer]\nurl = "http://p"\n[spoolman]\nurl = "http://s"\n'
            '[[tools]]\nindex = "zero"\nlocation = "A"\n',
            "tool index must be an integer",
        ),
    ],
)
def test_config_malformed_file_is_a_config_error(tmp_path, text, error):
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match=re.escape(error)):
        load(p, env={"PRUSALINK_API_KEY": "k"})


@pytest.mark.parametrize(
    "text, error",
    [
        ('[printer]\nurl = 5\n[spoolman]\nurl = "http://s"\n', "printer.url must be a string"),
        ('[printer]\nurl = "http://p"\n[spoolman]\nurl = 5\n', "spoolman.url must be a string"),
        (
            '[printer]\nurl = "http://p"\n[spoolman]\nurl = "http://s"\n'
            "[[tools]]\nindex = 0\nlocation = 5\n",
            "tool location must be a string",
        ),
    ],
)
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
    p.write_text(
        CONFIG_BASE.replace(
            "[printer]\n",
            '[printer]\nuuids = false\ngcode_preamble = ["M862.3 P \\"COREONEL\\""]\n',
        )
        + "[bridge]\nprinter_sync = false\ndry_run = false\npoll_idle = 0.5\n"
    )
    cfg = load(p, env={"PRUSALINK_API_KEY": "k"})
    assert cfg.printer_sync is False and cfg.dry_run is False and cfg.printers[0].uuids is False
    assert cfg.printers[0].gcode_preamble == ('M862.3 P "COREONEL"',)
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
    cfg = load(
        None,
        env={
            "PRUSALINK_URL": "http://p",
            "PRUSALINK_API_KEY": "k",
            "SPOOLMAN_URL": "http://s",
            "SPOOLMAN_PRUSALINK_LOCATION": "Core One L+",
            "SPOOLMAN_PRUSALINK_DRY_RUN": "false",
        },
    )
    assert cfg.printers[0].tools == {0: "Core One L+"} and cfg.dry_run is False
    assert cfg.state_file.as_posix() == "/data/state.json"
    with pytest.raises(ConfigError, match="SPOOLMAN_PRUSALINK_LOCATION"):
        load(None, env={"PRUSALINK_URL": "p", "PRUSALINK_API_KEY": "k", "SPOOLMAN_URL": "s"})
    with pytest.raises(ConfigError, match="true or false"):
        load(
            None,
            env={
                "PRUSALINK_URL": "p",
                "PRUSALINK_API_KEY": "k",
                "SPOOLMAN_URL": "s",
                "SPOOLMAN_PRUSALINK_LOCATION": "x",
                "SPOOLMAN_PRUSALINK_DRY_RUN": "maybe",
            },
        )


PRINTERS_BASE = (
    '[spoolman]\nurl = "http://s"\n'
    '[[printers]]\nname = "core"\nurl = "http://a"\napi_key_env = "CORE_KEY"\n'
    '[[printers.tools]]\nindex = 0\nlocation = "Core One L+"\n'
    '[[printers]]\nname = "mk4s"\nurl = "http://b/"\napi_key = "kb"\nuuids = false\n'
    '[[printers.tools]]\nindex = 0\nlocation = "MK4S"\n'
)
MK4S_TOOL = '[[printers.tools]]\nindex = 0\nlocation = "MK4S"\n'


def test_config_lists_several_printers(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(PRINTERS_BASE)
    core, mk4s = load(p, env={"CORE_KEY": "ka"}).printers
    assert (core.name, core.url, core.api_key, core.tools, core.uuids) == (
        "core",
        "http://a",
        "ka",
        {0: "Core One L+"},
        True,
    )
    assert (mk4s.name, mk4s.url, mk4s.api_key, mk4s.tools, mk4s.uuids) == (
        "mk4s",
        "http://b/",
        "kb",
        {0: "MK4S"},
        False,
    )


@pytest.mark.parametrize(
    "edit, error",
    [
        (('name = "core"\n', ""), "printers[0] needs a name"),
        (('name = "core"\n', 'name = "a b"\n'), "printers[0].name must match"),
        (('url = "http://a"\n', ""), "printers[core] needs a url"),
        (
            ('api_key_env = "CORE_KEY"\n', ""),
            "set printers[core].api_key or printers[core].api_key_env",
        ),
        (
            ('api_key_env = "CORE_KEY"\n', 'api_key_env = "NO_SUCH_KEY"\n'),
            "NO_SUCH_KEY, which isn't set",
        ),
        (
            ('api_key_env = "CORE_KEY"\n', 'api_key_env = "CORE_KEY"\napi_key = "x"\n'),
            "set printers[core].api_key or printers[core].api_key_env, not both",
        ),
        (
            ('[[printers.tools]]\nindex = 0\nlocation = "Core One L+"\n', ""),
            "printers[core] needs [[printers.tools]]",
        ),
        (('name = "mk4s"\n', 'name = "core"\n'), "printer 'core' is configured twice"),
        (
            ('url = "http://b/"\n', 'url = "http://a/"\n'),
            "printers 'core' and 'mk4s' have the same url",
        ),
        (
            ('url = "http://b/"\n', 'url = "HTTP://A:80"\n'),
            "printers 'core' and 'mk4s' have the same url",
        ),
        (('url = "http://b/"\n', 'url = "http://b:80a"\n'), "printers[mk4s].url isn't a valid"),
        (('url = "http://b/"\n', 'url = "http://b:99999"\n'), "printers[mk4s].url isn't a valid"),
        (('url = "http://b/"\n', 'url = "http://[::1"\n'), "printers[mk4s].url isn't a valid"),
        (
            (MK4S_TOOL, MK4S_TOOL + '[[printers.tools]]\nindex = 0\nlocation = "MK4S T2"\n'),
            "printers[mk4s]: tool 0 is configured twice",
        ),
        (
            (MK4S_TOOL, MK4S_TOOL + '[[printers.tools]]\nindex = 1\nlocation = "MK4S"\n'),
            "printers[mk4s]: location 'MK4S' is mapped to more than one tool",
        ),
        (
            ('location = "MK4S"\n', 'location = "Core One L+"\n'),
            "location 'Core One L+' is mapped to a tool on both 'core' and 'mk4s'",
        ),
        (("[spoolman]\n", '[printer]\nurl = "http://p"\n[spoolman]\n'), "not both"),
        (("[spoolman]\n", '[[tools]]\nindex = 0\nlocation = "X"\n[spoolman]\n'), "not both"),
        (("uuids = false\n", 'uuids = "no"\n'), "printers[mk4s].uuids must be true or false"),
    ],
)
def test_config_rejects_bad_printer_lists(tmp_path, edit, error):
    old, new = edit
    text = PRINTERS_BASE.replace(old, new, 1)
    assert text != PRINTERS_BASE or new == old
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match=re.escape(error)):
        load(p, env={"CORE_KEY": "ka"})


@pytest.mark.parametrize(
    "text, error",
    [
        ("printers = []\n", "[[printers]] needs at least one entry"),
        ("printers = 5\n", "printers must be a list of [[printers]] tables"),
        ("printers = [1]\n", "printers[0] must be a table"),
    ],
)
def test_config_rejects_a_printer_list_that_is_not_a_list_of_tables(tmp_path, text, error):
    p = tmp_path / "c.toml"
    p.write_text(text + '[spoolman]\nurl = "http://s"\n')
    with pytest.raises(ConfigError, match=re.escape(error)):
        load(p, env={})


@pytest.mark.parametrize(
    "var", ["PRUSALINK_URL", "PRUSALINK_API_KEY", "SPOOLMAN_PRUSALINK_LOCATION"]
)
def test_config_single_printer_variables_are_an_error_with_a_printer_list(tmp_path, var):
    p = tmp_path / "c.toml"
    p.write_text(PRINTERS_BASE)
    with pytest.raises(ConfigError, match=f"{var} configures a single"):
        load(p, env={"CORE_KEY": "ka", var: "x"})


def test_config_url_keys_keep_ipv6_hosts_apart_from_their_port(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        PRINTERS_BASE.replace('url = "http://a"\n', 'url = "http://[::1]:8080"\n', 1).replace(
            'url = "http://b/"\n', 'url = "http://[::1:8080]"\n', 1
        )
    )
    assert [x.url for x in load(p, env={"CORE_KEY": "ka"}).printers] == [
        "http://[::1]:8080",
        "http://[::1:8080]",
    ]  # Two printers, not "the same url".


@pytest.mark.parametrize(
    "text, env, error",
    [
        ("", {}, "set PRUSALINK_API_KEY or printer.api_key"),
        ("", {"PRUSALINK_API_KEY": "k"}, "set printer.url or PRUSALINK_URL"),
        ("", {"PRUSALINK_API_KEY": "k", "PRUSALINK_URL": "http://p"}, "set spoolman.url"),
        ("[printer]\napi_key = 5\n[spoolman]\nurl = 5\n", {}, "printer.api_key must be a string"),
        ("[printer]\nurl = 5\n[spoolman]\nurl = 5\n", {}, "printer.url must be a string"),
        ("printer = 5\nspoolman = 5\n", {}, "[printer] must be a table"),
        ("spoolman = 5\n[printer]\nurl = 5\n", {}, "[spoolman] must be a table"),
    ],
)
def test_config_single_printer_reports_errors_in_the_old_order(tmp_path, text, env, error):
    # The order of these first errors is what a bare start shows; it hasn't changed.
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ConfigError, match=re.escape(error)):
        load(p, env=env)


def test_config_single_printer_takes_a_name_and_api_key_env(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        CONFIG_BASE.replace("[printer]\n", '[printer]\nname = "core"\napi_key_env = "MY_KEY"\n')
    )
    cfg = load(p, env={"MY_KEY": "from-env"})
    assert (cfg.printers[0].name, cfg.printers[0].api_key) == ("core", "from-env")
    # PRUSALINK_API_KEY still wins, as before, and PRUSALINK_URL still overrides the file.
    cfg = load(p, env={"MY_KEY": "x", "PRUSALINK_API_KEY": "k", "PRUSALINK_URL": "http://e"})
    assert (cfg.printers[0].api_key, cfg.printers[0].url) == ("k", "http://e")
    # A file error is reported even when the environment would have supplied the key.
    for env in ({}, {"PRUSALINK_API_KEY": "k"}):
        with pytest.raises(ConfigError, match="MY_KEY, which isn't set"):
            load(p, env=env)
    p.write_text(CONFIG_BASE.replace("[printer]\n", '[printer]\nname = "core one"\n'))
    with pytest.raises(ConfigError, match="printer.name must match"):
        load(p, env={"PRUSALINK_API_KEY": "k"})


V2_STATE = {
    "version": 2,
    "tools": {"0": ToolState(boot_id=1, charged=100, active=7, assigned=7).to_json()},
    "outbox": [{"tool": 0, "spool_id": 7, "use_length": 50.0, "extruded_mm": 50}],
    "flagged": [7],
    "noticed": [],
    "runouts": {},
    "claimed": [],
}


def test_state_v2_migrates_to_the_only_printer(tmp_path):
    path = tmp_path / "state.json"
    runouts = {"8": "old text form", "9": {"text": "with weights", "net": 1000.0, "used": 10.0}}
    path.write_text(json.dumps({**V2_STATE, "runouts": runouts}))
    state = load_state(path, ["core"])
    assert list(state.printers) == ["core"]
    assert state.tools("core")[0].active == 7
    # Outbox records and runouts are stamped: flush() finds the record's tool by it.
    assert [r["printer"] for r in state.outbox] == ["core"]
    assert {k: r.printer for k, r in state.runouts.items()} == {8: "core", 9: "core"}
    assert (state.runouts[8].text, state.runouts[9].net) == ("old text form", 1000.0)
    assert state.flagged == [7]
    assert save_state(path, state)
    saved = json.loads(path.read_text())
    assert saved["version"] == 3 and "tools" not in saved
    assert saved["printers"]["core"]["tools"]["0"]["active"] == 7
    assert load_state(path, ["core"]) == state


def test_state_v2_is_refused_with_several_printers(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(V2_STATE))
    with pytest.raises(ValueError, match="single-printer bridge.*2 printers configured"):
        load_state(path, ["core", "mk4s"])
    path.write_text(json.dumps({**V2_STATE, "version": 1}))
    with pytest.raises(ValueError, match="unsupported state version 1"):
        load_state(path, ["core"])


def test_main_exits_2_on_a_state_file_it_cannot_migrate(tmp_path, monkeypatch, caplog):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(V2_STATE))
    p = tmp_path / "c.toml"
    p.write_text(PRINTERS_BASE + f'[bridge]\nstate_file = "{state}"\n')
    monkeypatch.setenv("CORE_KEY", "ka")
    assert main(["-c", str(p), "once"]) == 2
    assert "state: " in caplog.text and "single-printer bridge" in caplog.text


def test_main_exits_2_on_a_state_file_it_cannot_load(tmp_path, monkeypatch, caplog):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({**V2_STATE, "version": 1}))
    p = tmp_path / "c.toml"
    p.write_text(CONFIG_BASE + f'[bridge]\nstate_file = "{state}"\n')
    monkeypatch.setenv("PRUSALINK_API_KEY", "k")
    assert main(["-c", str(p), "once"]) == 2
    assert "state: " in caplog.text and "unsupported state version 1" in caplog.text


def test_bridge_keeps_and_warns_about_an_unconfigured_printer(tmp_path, caplog):
    # X14: a renamed printer leaves its old state behind, which is kept.
    cfg = make_config(tmp_path)
    old = PrinterState({0: ToolState(boot_id=1, charged=100, active=7, assigned=7)})
    t = httpx.MockTransport(lambda request: httpx.Response(404))
    bridge = Bridge(
        cfg,
        Spoolman("http://s", transport=t),
        {"default": PrusaLink("http://p", "k", transport=t)},
        State(printers={"old": old}),
    )
    assert "printer 'old' that isn't configured; keeping its state" in caplog.text
    assert bridge.printers[0].tools == {}
    assert bridge.state.printers["old"] == old


TAG = "8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"
MATERIAL_UUID = "0b1c6b8e-8d0f-4f8e-9a63-2b1f0d6c7e11"


def tag_material(instance=TAG, from_tag=True):
    return Material(
        "PLA",
        "#73767d",
        instance,
        MATERIAL_UUID if from_tag else None,
        nozzle_temperature=215,
        preheat_temperature=170,
        bed_temperature=60,
    )


@pytest.fixture
async def printer(tmp_path):
    bridge = make_bridge(tmp_path, lambda: FILAMENT, lambda: [])
    p = bridge.printers[0]
    p._model = printer_sync.PRINTER_MODELS["8.1.0"]
    yield p
    await bridge.aclose()


def spool(id, uuid=None):
    return parse_spool(spool_json(id, "Core One L+", uuid=uuid), "instance_uuid")


SETTLED = ToolState(active=7, assigned=7)


@pytest.mark.parametrize(
    ("st", "material", "spools", "want"),
    [
        # A tag on a spool with no UUID: adopt it.
        (SETTLED, tag_material(), [spool(7)], ("adopt", TAG)),
        # The case doesn't matter.
        (SETTLED, tag_material(TAG.upper()), [spool(7)], ("adopt", TAG)),
        # Not a tag: the bridge mints a UUID of its own.
        (SETTLED, tag_material(from_tag=False), [spool(7)], None),
        # Not a tag, but the bridge's last write kept it.
        (
            replace(SETTLED, written=f'M865 X R N"PLA" L0\nM865.1 I0 C"{TAG}"'),
            tag_material(from_tag=False),
            [spool(7)],
            ("keep", TAG),
        ),
        # A tag with no instance UUID.
        (SETTLED, tag_material(None), [spool(7)], None),
        # The active spool isn't certain: staged, unconfirmed, or provisional. The tag
        # isn't adopted, and needn't be kept: a tool loaded from a tag isn't written.
        (replace(SETTLED, assigned=8), tag_material(), [spool(7), spool(8)], None),
        (replace(SETTLED, swap=SwapFlag(7)), tag_material(), [spool(7)], None),
        (replace(SETTLED, provisional=Provisional(7, 10)), tag_material(), [spool(7)], None),
        # Not certain and a different UUID in Spoolman: drift, not a conflict.
        (replace(SETTLED, swap=SwapFlag(7)), tag_material(), [spool(7, "other")], None),
        # Another spool has the UUID: S12 or drift, not this spool's.
        (SETTLED, tag_material(), [spool(7), spool(8, TAG.upper())], None),
        # The spool has it already, or a different one.
        (SETTLED, tag_material(), [spool(7, TAG)], None),
        (SETTLED, tag_material(), [spool(7, "other")], ("conflict", TAG)),
    ],
)
async def test_tag_uuid(printer, st, material, spools, want):
    active = next(s for s in spools if s.id == 7)
    printer._seen_uuid[0] = ("before", 7, False)  # The bridge saw the tag appear.
    assert printer._tag_uuid(0, st, material, active, spools) == want


async def test_tag_uuid_already_loaded_at_start_is_not_trusted(printer):
    """The bridge didn't see the tag load: the spool assigned may not be the tag's."""
    for _ in range(2):
        # Not stored, and no conflict either.
        assert printer._tag_uuid(0, SETTLED, tag_material(), spool(7), [spool(7)]) is None
        assert printer._tag_uuid(0, SETTLED, tag_material(), spool(7, "x"), [spool(7)]) is None
    # A reload of the same tag that the bridge watched: adopted.
    printer._uuid_gap.add(0)
    assert printer._tag_uuid(0, SETTLED, tag_material(), spool(7), [spool(7)]) == ("adopt", TAG)
    # So is a different tag.
    printer._tag_uuid(
        0, SETTLED, tag_material("1f0e7d2a-3f6c-4d7b-9a43-6c1f0e7d2a11"), spool(7), [spool(7)]
    )
    assert printer._tag_uuid(0, SETTLED, tag_material(), spool(7), [spool(7)]) == ("adopt", TAG)


async def test_tag_uuid_not_on_tool_1_or_without_uuids(printer):
    assert printer._tag_uuid(1, SETTLED, tag_material(), spool(7), [spool(7)]) is None
    printer._model = printer_sync.PRINTER_MODELS["1.4.1"]
    assert printer._tag_uuid(0, SETTLED, tag_material(), spool(7), [spool(7)]) is None


async def test_tag_uuid_seen_with_another_spool_is_not_adopted(printer):
    """A runout reload keeps the old spool's tag: the next spool doesn't take its UUID."""
    old, new = spool(6), spool(7)
    printer._seen_uuid[0] = ("before", 6, False)
    # Spool 6's tag, seen while 6 was certainly loaded; its UUID isn't in Spoolman.
    assert printer._tag_uuid(0, ToolState(active=6, assigned=6), tag_material(), old, [old]) == (
        "adopt",
        TAG,
    )
    # A runout, a swap to 7 and a reload: the printer still shows 6's tag.
    assert printer._tag_uuid(0, SETTLED, tag_material(), new, [new]) is None
    # Still not on later polls.
    assert printer._tag_uuid(0, SETTLED, tag_material(), new, [new]) is None
    # A different tag on 7 is adopted.
    other = "1f0e7d2a-3f6c-4d7b-9a43-6c1f0e7d2a11"
    assert printer._tag_uuid(0, SETTLED, tag_material(other), new, [new]) == ("adopt", other)


async def test_tag_uuid_refused_for_an_archived_spool_is_not_proposed_again(printer):
    printer._seen_uuid[0] = ("before", 7, False)
    printer.bridge._refused_uuids.add((7, TAG))
    assert printer._tag_uuid(0, SETTLED, tag_material(), spool(7), [spool(7)]) is None


async def test_adopt_uuid_checks_archived_spools(tmp_path, caplog):
    """An archived spool with the tag's UUID keeps it; Spoolman is read once for it."""
    reads, writes = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/spool" and request.method == "GET":
            reads.append(request.url.params.get("allow_archived"))
            archived = spool_json(6, None, uuid=TAG.upper())
            archived["archived"] = True
            return httpx.Response(200, json=[archived, spool_json(7, "Core One L+")])
        writes.append(request)
        return httpx.Response(200, json={})

    cfg = make_config(tmp_path, dry_run=False)
    transport = httpx.MockTransport(handler)
    bridge = Bridge(
        config=cfg,
        spoolman=Spoolman(cfg.spoolman_url, uuid_field="instance_uuid", transport=transport),
        clients={"default": PrusaLink(cfg.printers[0].url, "k", transport=transport)},
    )
    caplog.set_level("INFO")
    assert await bridge._adopt_uuid(7, TAG) is False
    assert await bridge._adopt_uuid(7, TAG) is False
    assert reads == ["true"] and writes == []
    assert caplog.text.count("archived spool 6 has it") == 1
    await bridge.aclose()


@pytest.mark.parametrize("refused", [False, True])
async def test_failed_adoption_holds_the_write(printer, monkeypatch, refused):
    """A failed store waits for the next poll; a refusal lets the write mint instead."""

    async def fail(spool_id, value, store=True):
        if refused:
            printer.bridge._refused_uuids.add((spool_id, value))
        return False

    monkeypatch.setattr(printer.bridge, "_adopt_uuid", fail)
    printer._adopts = {0: (7, TAG, True)}
    printer._pending_writes = [0]
    await printer.adopt_uuids()
    assert printer._adopts == {}
    assert printer._pending_writes == ([0] if refused else [])


async def test_kept_uuid_is_checked_against_archived_spools_once(tmp_path):
    reads = []

    def handler(request: httpx.Request) -> httpx.Response:
        reads.append(request.url.params.get("allow_archived"))
        return httpx.Response(200, json=[spool_json(7, "Core One L+")])

    cfg = make_config(tmp_path, dry_run=False)
    transport = httpx.MockTransport(handler)
    bridge = Bridge(
        config=cfg,
        spoolman=Spoolman(cfg.spoolman_url, uuid_field="instance_uuid", transport=transport),
        clients={"default": PrusaLink(cfg.printers[0].url, "k", transport=transport)},
    )
    for _ in range(2):
        assert await bridge._adopt_uuid(7, TAG, store=False) is True
    assert reads == ["true"]  # Read once, and nothing written.
    await bridge.aclose()


async def test_written_uuid_seen_with_another_spool_is_not_kept(printer):
    """A UUID the bridge wrote for spool 6 isn't kept for spool 7 after a runout reload."""
    printer._seen_uuid[0] = (TAG, 6, False)
    st = replace(SETTLED, written=f'M865 X R N"PLA" L0\nM865.1 I0 C"{TAG}"')
    material = tag_material(from_tag=False)
    assert printer._tag_uuid(0, st, material, spool(7), [spool(7)]) is None


@pytest.mark.parametrize(
    ("tool", "material", "want"),
    [
        (0, tag_material(), True),
        # A tag with no instance UUID is still a tag.
        (0, tag_material(None), True),
        # A menu pick or the bridge's own write.
        (0, tag_material(from_tag=False), False),
        # Only tool 0 holds a tag's data.
        (1, tag_material(), False),
    ],
)
async def test_loaded_tag(printer, tool, material, want):
    assert printer._loaded_tag(tool, material, spool(7)) is want


async def test_loaded_tag_needs_uuids(printer):
    printer._model = printer_sync.PRINTER_MODELS["1.4.1"]
    assert printer._loaded_tag(0, tag_material(), spool(7)) is False


async def test_runout_leftover_is_not_a_loaded_tag(printer):
    """A runout reload keeps spool 6's tag with spool 7 active: that tag isn't 7's."""
    printer._seen_uuid[0] = (TAG, 6, True)
    assert printer._loaded_tag(0, tag_material(TAG.upper()), spool(7)) is False
    # Spool 6's own reload of it is its tag, as is another tag for spool 7.
    assert printer._loaded_tag(0, tag_material(), spool(6)) is True
    other = "1f0e7d2a-3f6c-4d7b-9a43-6c1f0e7d2a11"
    assert printer._loaded_tag(0, tag_material(other), spool(7)) is True
    # With no instance UUID, a leftover can't be told apart: it counts as loaded.
    assert printer._loaded_tag(0, tag_material(None), spool(7)) is True


@pytest.mark.parametrize(
    ("material", "active", "spools", "want"),
    [
        # Another spool's tag, whether or not the active spool has a UUID.
        (
            tag_material(),
            spool(7),
            [spool(8, TAG.upper())],
            [f"the loaded tag is spool 8's ({TAG})"],
        ),
        (tag_material(), spool(7, "x"), [spool(8, TAG)], [f"the loaded tag is spool 8's ({TAG})"]),
        # The active spool has another UUID.
        (tag_material(), spool(7, "x"), [], [f"uuid printer={TAG} spoolman=x"]),
        # The spool's own tag, a spool with no UUID, or a tag with none.
        (tag_material(), spool(7, TAG.upper()), [], []),
        (tag_material(), spool(7), [], []),
        (tag_material(None), spool(7, "x"), [spool(8, "y")], []),
    ],
)
async def test_tag_differences(printer, material, active, spools, want):
    assert printer._tag_differences(material, active, [active, *spools]) == want
