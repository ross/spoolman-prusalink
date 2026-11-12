import asyncio
import json
import logging
import os
import re
import signal
import sqlite3
from dataclasses import replace

import httpx
import pytest

from spoolman_prusalink import __main__ as main_module
from spoolman_prusalink.__main__ import main
from spoolman_prusalink.accounting import ToolState
from spoolman_prusalink.app import Bridge, Reachability, _log_failed, _log_ok
from spoolman_prusalink.config import Config, ConfigError, PrinterConfig, load
from spoolman_prusalink.db import Database, DatabaseError, read_ledger, read_state
from spoolman_prusalink.errors import BadAnswer, Unreachable
from spoolman_prusalink.models import Material
from spoolman_prusalink.prusalink import NoFilamentEndpoint, PrusaLink, parse_filament
from spoolman_prusalink.spoolman import FieldTypeError, Spoolman, parse_spool
from spoolman_prusalink.state import PrinterState, State

ENV_VARS = (
    "SPOOLMAN_PRUSALINK_CONFIG",
    "LOG_LEVEL",
    "PRUSALINK_API_KEY",
    "PRUSALINK_URL",
    "SPOOLMAN_URL",
    "SPOOLMAN_PRUSALINK_LOCATION",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Clear every variable config.py and __main__.py read, so the shell can't leak in."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


FILAMENT = {
    "boot_id": 3308742886,
    "tools": [
        {
            "index": 0,
            "extruded": 4310,
            "unload_count": 0,
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
            "tools": [FILAMENT["tools"][0], {"index": 1, "extruded": 0, "unload_count": 0}],
        }
    )
    assert r.boot_id == 5
    assert r.tools[0].material.instance_uuid.startswith("44c4")
    assert r.tools[0].unload_count == 0
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


async def test_filament_without_unload_count_is_unsupported():
    data = {"boot_id": 5, "tools": [{"index": 0, "extruded": 10}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=data))
    client = PrusaLink("http://printer", "k", transport=transport)
    with pytest.raises(NoFilamentEndpoint, match="no usage counters"):
        await client.filament()
    await client.aclose()


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


def test_parse_spool_ignores_malformed_extras_and_timestamps():
    data = spool_json(3, "Core One L+")
    data["extra"] = {
        "instance_uuid": "not json",
        "bridge_flag": None,
        "bridge_claim": "{",
    }
    data["last_used"] = "yesterday"
    s = parse_spool(data, "instance_uuid", "bridge_flag", "bridge_claim")
    assert (s.instance_uuid, s.flag, s.claim, s.last_used) == (None, None, False, None)
    data["extra"]["instance_uuid"] = json.dumps(5)  # Valid JSON, but not text.
    assert parse_spool(data, "instance_uuid").instance_uuid is None


@pytest.mark.parametrize("body", ["[]", "not json"])
async def test_use_length_without_a_used_weight(body):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    spoolman = Spoolman("http://spoolman", transport=transport)
    assert await spoolman.use_length(3, 10.0) is None
    await spoolman.aclose()


async def test_ensure_field_refuses_a_field_of_another_type():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"  # Nothing is created over it.
        return httpx.Response(200, json=[{"key": "instance_uuid", "field_type": "integer"}])

    spoolman = Spoolman("http://spoolman", transport=httpx.MockTransport(handler))
    with pytest.raises(FieldTypeError, match="instance_uuid.*isn't a text field"):
        await spoolman.ensure_field("instance_uuid", "Instance UUID")
    await spoolman.aclose()


async def test_job_display_name_when_the_job_ended_between_reads():
    transport = httpx.MockTransport(lambda request: httpx.Response(204))
    client = PrusaLink("http://printer", "k", transport=transport)
    assert await client.job_display_name() is None
    await client.aclose()


def make_config(tmp_path, **kw):
    printer = PrinterConfig(
        name="default", url="http://printer", api_key="k", tools={0: "Core One L+"}
    )
    return Config(
        spoolman_url="http://spoolman",
        printers=(printer,),
        db_file=tmp_path / "bridge.db",
        **kw,
    )


def make_bridge(tmp_path, filament, spools):
    """A bridge on fake HTTP whose only write is a charge, which Spoolman accepts."""

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
        if request.method == "PUT" and request.url.path.endswith("/use"):
            return httpx.Response(200, json={})
        assert request.method == "GET", f"unexpected write: {request.method} {request.url}"
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    cfg = make_config(tmp_path, flag_field=None, claim_field=None)
    db = Database.open(cfg.db_file)
    return Bridge(
        config=cfg,
        spoolman=Spoolman(cfg.spoolman_url, uuid_field="instance_uuid", transport=transport),
        clients={"default": PrusaLink(cfg.printers[0].url, "k", transport=transport)},
        state=db.load(),
        db=db,
    )


async def test_poll_charges_and_saves(tmp_path, caplog):
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
    assert "[Core One L+] CHARGE spool 7" in caplog.text
    assert "≈ +2.98 g" in caplog.text

    ledger = read_ledger(tmp_path / "bridge.db")
    charges = [r for r in ledger if "use_length" in r]
    assert [(r["spool_id"], r["use_length"]) for r in charges] == [(7, 1000)]
    assert charges[0]["applied"]
    # An event's record has its fields beside its message.
    [baseline] = [r for r in ledger if "event" in r]
    assert {k: baseline[k] for k in ("event", "extruded", "spool", "message")} == {
        "event": "baseline",
        "extruded": 4310,
        "spool": 7,
        "message": "baseline extruded=4310 spool=7",
    }
    assert bridge.printers[0].tools[0].charged == 5310
    assert read_state(tmp_path / "bridge.db").tools("default")[0].charged == 5310
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
        cfg,
        Spoolman("http://s", transport=t),
        {"default": PrusaLink("http://p", "k", transport=t)},
        Database.open(cfg.db_file),
    )
    await bridge.poll()
    assert "printer read failed" in caplog.text
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
        cfg,
        Spoolman("http://s", transport=t),
        {"default": PrusaLink("http://p", "k", transport=t)},
        Database.open(cfg.db_file),
        clock=lambda: now["v"],
    )
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
    # The printer now answers, badly: the trouble changes kind, which logs at WARNING.
    outage = [
        (r.levelname, r.getMessage().split(":")[0])
        for r in caplog.records
        if r.getMessage().startswith("printer") and r.levelno >= logging.INFO
    ]
    assert outage == [
        ("WARNING", "printer unreachable"),
        ("INFO", "printer still unreachable after 3h 00m"),
        ("WARNING", "printer read failed, skipping poll"),
    ]
    assert bridge.printers[0].reachable
    await bridge.aclose()


async def test_printer_kind_change_and_bad_answer_reporting(tmp_path, caplog):
    mode = {"v": "refuse"}
    now = {"v": 0.0}

    def handler(request):
        if mode["v"] == "refuse":
            raise httpx.ConnectError("refused")
        return httpx.Response(401)

    cfg = make_config(tmp_path)
    t = httpx.MockTransport(handler)
    bridge = Bridge(
        cfg,
        Spoolman("http://s", transport=t),
        {"default": PrusaLink("http://p", "k", transport=t)},
        Database.open(cfg.db_file),
        clock=lambda: now["v"],
    )
    printer = bridge.printers[0]
    caplog.set_level("DEBUG")

    def lines(text):
        return [
            (r.levelname, r.getMessage().split(":")[0])
            for r in caplog.records
            if text in r.getMessage()
        ]

    await bridge.poll()
    assert not printer.reachable
    mode["v"] = "bad"  # Answering badly is a new kind of trouble, and counts as up.
    await bridge.poll()
    assert printer.reachable
    assert bridge.interval() == cfg.poll_printing  # Up, and not yet read.
    now["v"] += 60
    await bridge.poll()  # Same kind again: DEBUG.
    now["v"] += 3600
    await bridge.poll()  # An hour on: the reminder.
    mode["v"] = "refuse"
    await bridge.poll()
    mode["v"] = "bad"
    await bridge.poll()
    assert [x for x in lines("printer") if x[0] != "DEBUG"] == [
        ("WARNING", "printer unreachable"),
        ("WARNING", "printer read failed, skipping poll"),
        ("INFO", "printer still giving bad answers after 1h 01m"),
        ("WARNING", "printer unreachable"),
        ("WARNING", "printer read failed, skipping poll"),
    ]
    assert ("DEBUG", "printer read failed, skipping poll") in lines("skipping")
    await bridge.aclose()


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
    assert cfg.printers[0].length_factor == 1.0
    # P1 and C2: poll every 30 s while printing and every 60 s otherwise.
    assert cfg.poll_printing == 30
    assert cfg.poll_idle == 60
    assert cfg.printers[0].sync_delay == 0

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
    p.write_text(p.read_text() + '[bridge]\nflag_field = "Bad Key"\n')
    with pytest.raises(ConfigError, match="flag_field"):
        load(p, env={"PRUSALINK_API_KEY": "secret"})


CONFIG_BASE = (
    '[printer]\nurl = "http://p"\n[spoolman]\nurl = "http://s"\n'
    '[[tools]]\nindex = 0\nlocation = "Core One L+"\n'
)


@pytest.mark.parametrize(
    "text, error",
    [
        ('[printer]\nprinter_sync = "false"\n', "printer.printer_sync must be true or false"),
        ('[printer]\nflag_drift = "no"\n', "printer.flag_drift must be true or false"),
        ("[printer]\nsync_drift = 1\n", "printer.sync_drift must be true or false"),
        ('[printer]\ngcode_preamble = "M862.3"\n', "gcode_preamble must be a list of strings"),
        ('[printer]\ngcode_preamble = ["M862.3", 1]\n', "gcode_preamble must be a list of strings"),
        ("[bridge]\npoll_idle = 0\n", "bridge.poll_idle must be greater than 0"),
        ("[printer]\nsync_delay = -1\n", "printer.sync_delay must be 0 or more"),
        ('[printer]\nsync_delay = "5m"\n', "printer.sync_delay must be a number"),
        ("[bridge]\npoll_printing = -5\n", "bridge.poll_printing must be greater than 0"),
        ('[bridge]\npoll_idle = "soon"\n', "bridge.poll_idle must be a number"),
        ('[printer]\nlength_factor = "x"\n', "printer.length_factor must be a number"),
        ("[bridge]\npoll_idle = nan\n", "bridge.poll_idle must be a finite number"),
        ("[bridge]\npoll_printing = inf\n", "bridge.poll_printing must be a finite number"),
        (
            "[bridge]\npoll_printing = -nan\n",
            "bridge.poll_printing must be a finite number",
        ),
        ("[printer]\nlength_factor = nan\n", "printer.length_factor must be a finite number"),
        ("[printer]\nlength_factor = inf\n", "printer.length_factor must be a finite number"),
        (
            "[printer]\nlength_factor = 1" + "0" * 400 + "\n",
            "printer.length_factor must be a finite number",
        ),
        (
            "[bridge]\npoll_idle = 1" + "0" * 400 + "\n",
            "bridge.poll_idle must be a finite number",
        ),
        (
            "[printer]\nsync_delay = 1" + "0" * 400 + "\n",
            "printer.sync_delay must be a finite number",
        ),
        ("[bridge]\nuuid_field = 5\n", "bridge.uuid_field must be a string"),
        ("[bridge]\nflag_field = true\n", "bridge.flag_field must be a string"),
        ('[bridge]\nclaim_field = ["a"]\n', "bridge.claim_field must be a string"),
        ("[bridge]\ndb_file = 5\n", "bridge.db_file must be a string"),
        ("[bridge]\neviction_location = 5\n", "bridge.eviction_location must be a string"),
        ('[printer]\nsync_file = "a/b.txt"\n', "printer.sync_file must be a plain .gcode file"),
        ("[printer]\nlength_factor = 3\n", "printer.length_factor must be in (0, 2], got 3"),
        ("[printer]\nsync_storage = 5\n", "printer.sync_storage must be a string"),
        ("[printer]\nsync_file = 5\n", "printer.sync_file must be a string"),
        ("[printer]\napi_key = 5\n", "printer.api_key must be a string"),
        *(
            (f"[bridge]\n{key} = 1\n", f"bridge.{key} is a printer setting")
            for key in ("length_factor", "flag_drift", "printer_sync", "sync_delay", "sync_drift")
        ),
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
        ("[bridge]\npoll_idle = 1" + "0" * 5000 + "\n", "can't parse"),
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
        (
            'tools = 5\n[printer]\nurl = "http://p"\n[spoolman]\nurl = "http://s"\n',
            "tools must be [[tools]] entries",
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
        (
            '[printer]\nurl = "http://p"\n[spoolman]\nurl = 5\n'
            '[[tools]]\nindex = 0\nlocation = "X"\n',
            "spoolman.url must be a string",
        ),
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


def test_main_exits_2_on_a_malformed_config(tmp_path, clean_env, monkeypatch, caplog):
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
            '[printer]\nprinter_sync = false\ngcode_preamble = ["M862.3 P \\"COREONEL\\""]\n',
        )
        + "[bridge]\npoll_idle = 0.5\n"
    )
    cfg = load(p, env={"PRUSALINK_API_KEY": "k"})
    assert cfg.printers[0].printer_sync is False
    assert cfg.printers[0].gcode_preamble == ('M862.3 P "COREONEL"',)
    assert cfg.poll_idle == 0.5


async def test_length_factor_scales_charges(tmp_path):
    extruded = {"v": 1000}

    def filament():
        data = json.loads(json.dumps(FILAMENT))
        data["tools"][0]["extruded"] = extruded["v"]
        return data

    bridge = make_bridge(tmp_path, filament, lambda: [spool_json(7, "Core One L+")])
    printer = bridge.printers[0]
    printer.config = replace(printer.config, length_factor=1.05)
    await bridge.poll()
    extruded["v"] = 2000
    await bridge.poll()
    charge = next(r for r in read_ledger(tmp_path / "bridge.db") if "use_length" in r)
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
        },
    )
    assert cfg.printers[0].tools == {0: "Core One L+"}
    assert cfg.db_file.as_posix() == "/data/bridge.db"
    with pytest.raises(ConfigError, match="SPOOLMAN_PRUSALINK_LOCATION"):
        load(None, env={"PRUSALINK_URL": "p", "PRUSALINK_API_KEY": "k", "SPOOLMAN_URL": "s"})


PRINTERS_BASE = (
    '[spoolman]\nurl = "http://s"\n'
    '[[printers]]\nname = "core"\nurl = "http://a"\napi_key_env = "CORE_KEY"\n'
    '[[printers.tools]]\nindex = 0\nlocation = "Core One L+"\n'
    '[[printers]]\nname = "mk4s"\nurl = "http://b/"\napi_key = "kb"\n'
    '[[printers.tools]]\nindex = 0\nlocation = "MK4S"\n'
)
MK4S_TOOL = '[[printers.tools]]\nindex = 0\nlocation = "MK4S"\n'


def test_config_lists_several_printers(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(PRINTERS_BASE)
    core, mk4s = load(p, env={"CORE_KEY": "ka"}).printers
    assert (core.name, core.url, core.api_key, core.tools) == (
        "core",
        "http://a",
        "ka",
        {0: "Core One L+"},
    )
    assert (mk4s.name, mk4s.url, mk4s.api_key, mk4s.tools) == (
        "mk4s",
        "http://b/",
        "kb",
        {0: "MK4S"},
    )


def test_config_keeps_each_printers_settings(tmp_path):
    """A printer setting applies to the entry that sets it; the others keep the default."""
    p = tmp_path / "c.toml"
    p.write_text(
        PRINTERS_BASE.replace(
            'api_key = "kb"\n', 'api_key = "kb"\nlength_factor = 1.05\nprinter_sync = false\n'
        )
    )
    core, mk4s = load(p, env={"CORE_KEY": "ka"}).printers
    assert (core.length_factor, core.printer_sync) == (1.0, True)
    assert (mk4s.length_factor, mk4s.printer_sync) == (1.05, False)


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
        ("", {"PRUSALINK_API_KEY": "k", "PRUSALINK_URL": "http://p"}, "configure [[tools]]"),
        (
            "",
            {"PRUSALINK_API_KEY": "k", "PRUSALINK_URL": "p", "SPOOLMAN_PRUSALINK_LOCATION": "X"},
            "set spoolman.url",
        ),
        ("[printer]\napi_key = 5\n[spoolman]\nurl = 5\n", {}, "printer.api_key must be a string"),
        ("[printer]\nurl = 5\n[spoolman]\nurl = 5\n", {}, "printer.url must be a string"),
        ("printer = 5\nspoolman = 5\n", {}, "[printer] must be a table"),
        ("spoolman = 5\n[printer]\nurl = 5\n", {}, "printer.url must be a string"),
        (
            'spoolman = 5\n[printer]\nurl = "http://p"\n[[tools]]\nindex = 0\nlocation = "X"\n',
            {"PRUSALINK_API_KEY": "k"},
            "[spoolman] must be a table",
        ),
    ],
)
def test_config_reports_the_printer_then_spoolman(tmp_path, text, env, error):
    """The first error is the printer's, then Spoolman's: within the printer, a value of
    the wrong type before a missing one."""
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


def test_main_exits_2_on_a_database_it_cannot_load(tmp_path, clean_env, monkeypatch, caplog):
    db = tmp_path / "bridge.db"
    Database.open(db).close()
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    conn.close()
    p = tmp_path / "c.toml"
    p.write_text(CONFIG_BASE + f'[bridge]\ndb_file = "{db}"\n')
    monkeypatch.setenv("PRUSALINK_API_KEY", "k")
    assert main(["-c", str(p), "once"]) == 2
    assert "database: " in caplog.text and "unsupported schema version '0'" in caplog.text


def test_main_exits_2_while_another_bridge_holds_the_database(
    tmp_path, clean_env, monkeypatch, caplog
):
    db = tmp_path / "bridge.db"
    p = tmp_path / "c.toml"
    p.write_text(CONFIG_BASE + f'[bridge]\ndb_file = "{db}"\n')
    monkeypatch.setenv("PRUSALINK_API_KEY", "k")
    running = Database.open(db)
    assert main(["-c", str(p), "once"]) == 2
    assert f"database: {db}.lock is locked" in caplog.text
    running.close()


def test_bridge_keeps_and_warns_about_an_unconfigured_printer(tmp_path, caplog):
    # X14: a renamed printer leaves its old state behind, which is kept.
    cfg = make_config(tmp_path)
    old = PrinterState({0: ToolState(boot_id=1, charged=100, active=7, assigned=7)})
    t = httpx.MockTransport(lambda request: httpx.Response(404))
    bridge = Bridge(
        cfg,
        Spoolman("http://s", transport=t),
        {"default": PrusaLink("http://p", "k", transport=t)},
        Database.open(cfg.db_file),
        State(printers={"old": old}),
    )
    assert "printer 'old' that isn't configured; keeping its state" in caplog.text
    assert bridge.printers[0].tools == {}
    assert bridge.state.printers["old"] == old
    bridge.db.close()


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


def spool(id, uuid=None):
    return parse_spool(spool_json(id, "Core One L+", uuid=uuid), "instance_uuid")


SETTLED = ToolState(active=7, assigned=7)


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

    cfg = make_config(tmp_path)
    transport = httpx.MockTransport(handler)
    bridge = Bridge(
        config=cfg,
        spoolman=Spoolman(cfg.spoolman_url, uuid_field="instance_uuid", transport=transport),
        clients={"default": PrusaLink(cfg.printers[0].url, "k", transport=transport)},
        db=Database.open(cfg.db_file),
    )
    caplog.set_level("INFO")
    assert await bridge.uuids.adopt(7, TAG) is False
    assert reads == ["true"] and writes == []
    assert caplog.text.count("archived spool 6 has it") == 1
    await bridge.aclose()


async def test_kept_uuid_is_checked_against_archived_spools_once(tmp_path):
    reads = []

    def handler(request: httpx.Request) -> httpx.Response:
        reads.append(request.url.params.get("allow_archived"))
        return httpx.Response(200, json=[spool_json(7, "Core One L+")])

    cfg = make_config(tmp_path)
    transport = httpx.MockTransport(handler)
    bridge = Bridge(
        config=cfg,
        spoolman=Spoolman(cfg.spoolman_url, uuid_field="instance_uuid", transport=transport),
        clients={"default": PrusaLink(cfg.printers[0].url, "k", transport=transport)},
        db=Database.open(cfg.db_file),
    )
    for _ in range(2):
        assert await bridge.uuids.adopt(7, TAG, store=False) is True
    assert reads == ["true"]  # Read once, and nothing written.
    await bridge.aclose()


class FakeBridge:
    """Stands in for Bridge in `main`: it records what the entry point asks of it."""

    def __init__(self, cfg):
        self.config, self.polls, self.closed, self.stop = cfg, 0, False, None

    async def poll(self):
        self.polls += 1

    async def run(self, stop):
        self.stop = stop
        os.kill(os.getpid(), signal.SIGTERM)  # As `docker stop` does.
        await asyncio.wait_for(stop.wait(), timeout=5)

    async def aclose(self):
        self.closed = True


@pytest.fixture
def fake_bridge(monkeypatch, clean_env):
    made = []

    def from_config(cfg):
        made.append(FakeBridge(cfg))
        return made[-1]

    monkeypatch.setattr(Bridge, "from_config", from_config)
    monkeypatch.setenv("PRUSALINK_API_KEY", "k")
    return made


def test_main_once_polls_once_and_closes_the_bridge(tmp_path, fake_bridge):
    p = tmp_path / "c.toml"
    p.write_text(CONFIG_BASE)
    assert main(["-c", str(p), "once"]) == 0
    (bridge,) = fake_bridge
    assert (bridge.polls, bridge.closed, bridge.config.spoolman_url) == (1, True, "http://s")


def test_main_run_stops_on_sigterm_and_closes_the_bridge(tmp_path, fake_bridge):
    p = tmp_path / "c.toml"
    p.write_text(CONFIG_BASE)
    assert main(["-c", str(p)]) == 0  # `run` is the default command.
    (bridge,) = fake_bridge
    assert (bridge.polls, bridge.closed, bridge.stop.is_set()) == (0, True, True)


def test_main_reads_the_default_config_path_when_it_exists(tmp_path, fake_bridge, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_BASE)
    monkeypatch.setattr(main_module, "DEFAULT_CONFIG", str(p))
    assert main(["once"]) == 0
    assert fake_bridge[0].config.spoolman_url == "http://s"


@pytest.mark.parametrize(
    ("level", "httpx_level"), [("debug", logging.NOTSET), ("info", logging.WARNING)]
)
def test_main_quiets_httpx_unless_debugging(tmp_path, fake_bridge, caplog, level, httpx_level):
    # pytest's handlers make basicConfig a no-op, so the root level is set here.
    caplog.set_level(level.upper())
    httpx_logger = logging.getLogger("httpx")
    before = httpx_logger.level
    httpx_logger.setLevel(logging.NOTSET)
    try:
        assert main(["--log-level", level, "-c", str(tmp_path / "missing.toml"), "once"]) == 2
        assert httpx_logger.level == httpx_level
    finally:
        httpx_logger.setLevel(before)


async def test_from_config_opens_the_database_and_builds_the_clients(tmp_path):
    cfg = make_config(tmp_path)
    bridge = Bridge.from_config(cfg)
    try:
        assert [p.name for p in bridge.printers] == ["default"]
        assert set(bridge.clients) == {"default"}
        assert bridge.db is not None and (tmp_path / "bridge.db").exists()
        with pytest.raises(DatabaseError, match="locked"):  # Held until aclose.
            Database.open(cfg.db_file)
    finally:
        await bridge.aclose()
    Database.open(cfg.db_file).close()


async def test_from_config_passes_the_clock_and_transports(tmp_path, caplog):
    cfg = make_config(tmp_path)
    seen: list[str] = []
    now = [3600.0]

    def refuse(name):
        def handler(request):
            seen.append(name)
            raise httpx.ConnectError("refused")

        return httpx.MockTransport(handler)

    bridge = Bridge.from_config(
        cfg,
        clock=lambda: now[0],
        spoolman_transport=refuse("spoolman"),
        printer_transports={"default": refuse("printer")},
    )
    try:
        await bridge.poll()  # The printer read fails, so Spoolman isn't read.
        assert seen == ["printer"]
        now[0] += 3600  # An hour on, by the clock passed in: the reminder.
        caplog.set_level("INFO")
        await bridge.poll()
        assert "printer still unreachable after 1h 00m" in caplog.text
        with pytest.raises(Unreachable):
            await bridge.spoolman.spools()
        assert seen == ["printer", "printer", "spoolman"]
    finally:
        await bridge.aclose()


def reach_lines(caplog):
    return [
        (r.levelname, r.getMessage().split(":")[0])
        for r in caplog.records
        if r.name == "spoolman_prusalink" and r.levelno >= logging.INFO
    ]


def test_kind_change_restarts_the_clock(caplog):
    now = {"v": 0.0}
    reach = Reachability("svc", clock=lambda: now["v"])
    caplog.set_level("INFO")
    for t, unreachable in [(0, True), (3600, True), (7000, False), (7200, False)]:
        now["v"] = t
        _log_failed(reach, BadAnswer("boom"), unreachable=unreachable)
    now["v"] = 7260
    _log_ok(reach)
    assert reach_lines(caplog) == [
        ("WARNING", "svc unreachable"),
        ("INFO", "svc still unreachable after 1h 00m"),
        ("WARNING", "svc read failed, skipping poll"),
        ("WARNING", "svc reads working again after 4m"),
    ]


def test_bad_answers_then_unreachable_measures_the_unreachable_span(caplog):
    now = {"v": 0.0}
    reach = Reachability("svc", clock=lambda: now["v"])
    caplog.set_level("INFO")
    _log_failed(reach, BadAnswer("boom"), unreachable=False)
    now["v"] = 3 * 3600
    _log_failed(reach, BadAnswer("boom"), unreachable=False)  # The reminder.
    now["v"] += 10
    _log_failed(reach, Unreachable("off"), unreachable=True)
    now["v"] += 60
    _log_ok(reach)
    assert reach_lines(caplog) == [
        ("WARNING", "svc read failed, skipping poll"),
        ("INFO", "svc still giving bad answers after 3h 00m"),
        ("WARNING", "svc unreachable"),
        ("WARNING", "svc reachable again after 1m"),
    ]
