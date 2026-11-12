import tomllib
from pathlib import Path

import pytest

from simulator.config import ConfigError, bridge_config, load, locations

EXAMPLE = Path(__file__).parent.parent / "sim.example.toml"


def write(tmp_path, text: str) -> Path:
    path = tmp_path / "sim.toml"
    path.write_text(text)
    return path


def printer(extra: str = "", model: str = "8.1.0", name: str = "p") -> str:
    return f'[[printers]]\nname = "{name}"\nmodel = "{model}"\n{extra}\n'


def test_example_loads():
    cfg = load(EXAMPLE)
    assert [p.name for p in cfg.printers] == ["core-one-l", "mk4s", "indx"]
    assert [p.port for p in cfg.printers] == [8001, 8002, 8003]
    core, mk4s, indx = cfg.printers
    assert core.tools[0].material == {
        "name": "PLA",
        "color": "#73737b",
        "instance_uuid": "44c406a5-c6f5-4440-bfba-e8060564b717",
    }
    assert core.tools[0].extruded == 4310
    assert mk4s.tools[0].material is None
    assert [t.index for t in indx.tools] == [0, 1, 2, 3, 4]
    assert [t.enabled for t in indx.tools] == [True, True, True, False, True]


def test_defaults(tmp_path):
    cfg = load(write(tmp_path, printer()))
    assert (cfg.host, cfg.control_port, cfg.settings) == ("127.0.0.1", 8000, {})
    p = cfg.printers[0]
    assert (p.port, p.api_key, p.firmware, p.auto_accept_model_check) == (8001, "sim-p", None, None)
    assert [t.index for t in p.tools] == [0]
    assert p.tools[0].material is None


def test_settings_overrides(tmp_path):
    cfg = load(write(tmp_path, "time_scale = 10\nstart_blip = false\n" + printer()))
    assert cfg.settings == {"time_scale": 10.0, "start_blip": False}


def test_normalizes_color_and_uuid(tmp_path):
    uuid = "44C406A5-C6F5-4440-BFBA-E8060564B717"
    material = f'{{ name = "PLA", color = "#73737B", instance_uuid = "{uuid}" }}'
    tool = f"[[printers.tools]]\nindex = 0\nmaterial = {material}\n"
    loaded = load(write(tmp_path, printer(tool))).printers[0].tools[0].material
    assert loaded["color"] == "#73737b"
    assert loaded["instance_uuid"] == uuid.lower()


def tool(body: str) -> str:
    return "[[printers.tools]]\n" + body + "\n"


BAD = {
    "unknown top key": ("bogus = 1\n" + printer(), "bogus isn't a known key"),
    "unknown printer key": (printer("bogus = 1"), "printers[0].bogus isn't a known key"),
    "unknown tool key": (printer(tool("index = 0\nbogus = 1")), "printers[0].tools[0].bogus"),
    "unknown material key": (
        printer(tool('index = 0\nmaterial = { name = "PLA", bogus = 1 }')),
        "printers[0].tools[0].material.bogus",
    ),
    "bad name": (printer(name="a b"), "printers[0].name must match"),
    "missing name": ("[[printers]]\nmodel = '8.1.0'\n", "printers[0] needs a name"),
    "unknown model": (printer(model="9.9.9"), "printers[0].model must be one of"),
    "model type": ("[[printers]]\nname = 'p'\nmodel = 8\n", "printers[0].model must be a string"),
    "bad firmware": (printer('firmware = "nope"'), "printers[0].firmware must be one of"),
    "bad auto accept": (
        printer('auto_accept_model_check = "yes"'),
        "printers[0].auto_accept_model_check must be true or false",
    ),
    "setting type bool": ('start_blip = "no"\n' + printer(), "start_blip must be true or false"),
    "setting type float": ('time_scale = "fast"\n' + printer(), "time_scale must be a number"),
    "setting bool as number": ("feed_ratio = true\n" + printer(), "feed_ratio must be a number"),
    "load rate zero": ("load_rate_mm_s = 0\n" + printer(), "load_rate_mm_s must be greater than 0"),
    "print rate zero": (
        "print_rate_mm_s = 0\n" + printer(),
        "print_rate_mm_s must be greater than 0",
    ),
    "negative number": ("purge_mm = -1\n" + printer(), "purge_mm must be a number of at least 0"),
    "time scale zero": ("time_scale = 0\n" + printer(), "time_scale must be greater than 0"),
    "port as string": (printer('port = "80"'), "printers[0].port must be an integer"),
    "port is control port": (printer("port = 8000"), "printers[0].port 8000 is already used"),
    "duplicate port": (
        printer("port = 9000") + printer("port = 9000", name="q"),
        "printers[1].port 9000 is already used by printers[0]",
    ),
    "default port clash": (
        printer("port = 8002") + printer(name="q"),
        "printers[1].port 8002 is already used by printers[0]",
    ),
    "duplicate name": (printer() + printer(), "printers[1].name 'p' is configured twice"),
    "index type": (printer(tool('index = "0"')), "printers[0].tools[0].index must be an integer"),
    "index missing": (printer(tool("enabled = true")), "printers[0].tools[0] needs an index"),
    "negative index": (
        printer(tool("index = -1")),
        "printers[0].tools[0].index must be at least 0",
    ),
    "index above model": (
        printer(tool("index = 1"), model="1.4.1"),
        "printers[0].tools[0].index must be below 1",
    ),
    "duplicate index": (
        printer(tool("index = 0") + tool("index = 0"), model="3.1.0"),
        "printers[0].tools[1].index 0 is configured twice",
    ),
    "too many tools": (
        printer(tool("index = 0") + tool("index = 1"), model="1.4.1"),
        "printers[0].tools has 2 entries",
    ),
    "material not a table": (
        printer(tool('index = 0\nmaterial = "PLA"')),
        "printers[0].tools[0].material must be a table",
    ),
    "material without name": (
        printer(tool('index = 0\nmaterial = { color = "#000000" }')),
        "printers[0].tools[0].material needs a name",
    ),
    "material name pattern": (
        printer(tool('index = 0\nmaterial = { name = "TOOLONGNAME" }')),
        "material.name must match",
    ),
    "bad color": (
        printer(tool('index = 0\nmaterial = { name = "PLA", color = "red" }')),
        "material.color must look like #rrggbb",
    ),
    "bad uuid": (
        printer(tool('index = 0\nmaterial = { name = "PLA", instance_uuid = "x" }')),
        "material.instance_uuid isn't a UUID",
    ),
    "uuid on a model without uuids": (
        printer(
            tool(
                'index = 0\nmaterial = { name = "PLA", '
                'instance_uuid = "44c406a5-c6f5-4440-bfba-e8060564b717" }'
            ),
            model="1.4.1",
        ),
        "needs a model that holds UUIDs",
    ),
    "negative extruded": (printer(tool("index = 0\nextruded = -1")), "extruded must be at least 0"),
    "negative unload count": (
        printer(tool("index = 0\nunload_count = -1")),
        "unload_count must be at least 0",
    ),
    "float unload count": (
        printer(tool("index = 0\nunload_count = 1.5")),
        "unload_count must be an integer",
    ),
    "at unload above extruded": (
        printer(tool("index = 0\nextruded = 10\nextruded_at_unload = 11")),
        "extruded_at_unload can't exceed extruded",
    ),
    "nan setting": ("feed_ratio = nan\n" + printer(), "must be a finite number"),
    "huge setting": ("feed_ratio = 1" + "0" * 400 + "\n" + printer(), "must be a finite number"),
    "port out of range": (printer("port = 70000"), "printers[0].port must be between 1 and 65535"),
    "tool not a table": (
        "[[printers]]\nname = 'p'\nmodel = '8.1.0'\ntools = [1]\n",
        "printers[0].tools[0] must be a table",
    ),
    "tools not a list": (printer("tools = 3"), "printers[0].tools must be a list"),
    "tools empty": (printer("tools = []"), "printers[0].tools must be a list"),
    "printer not a table": ("printers = [1]\n", "printers[0] must be a table"),
    "printers not a list": ("printers = 1\n", "printers must be a list"),
    "missing model": ("[[printers]]\nname = 'p'\n", "printers[0] needs a model"),
    "empty api key": (printer('api_key = ""'), "printers[0].api_key can't be empty"),
}


@pytest.mark.parametrize("case", list(BAD))
def test_errors(tmp_path, case):
    text, message = BAD[case]
    with pytest.raises(ConfigError) as e:
        load(write(tmp_path, text))
    assert message in str(e.value)


def test_accepts_each_firmware(tmp_path):
    for firmware in ("full", "no_counters", "no_endpoint"):
        cfg = load(write(tmp_path, printer(f'firmware = "{firmware}"')))
        assert cfg.printers[0].firmware == firmware


def test_unparseable_file(tmp_path):
    with pytest.raises(ConfigError, match="can't parse"):
        load(write(tmp_path, "this is = = not toml"))


def test_locations(tmp_path):
    cfg = load(EXAMPLE)
    locs = locations(cfg)
    assert [(loc.printer, loc.tool, loc.location, loc.uuids) for loc in locs] == [
        ("core-one-l", 0, "core-one-l", True),
        ("mk4s", 0, "mk4s", False),
        ("indx", 0, "indx T1", False),
        ("indx", 1, "indx T2", False),
        ("indx", 2, "indx T3", False),
        ("indx", 3, "indx T4", False),
        ("indx", 4, "indx T5", False),
    ]


def test_locations_carry_the_configured_material_and_uuid():
    locs = {loc.location: loc for loc in locations(load(EXAMPLE))}
    core = locs["core-one-l"]
    assert (core.material, core.instance_uuid) == ("PLA", "44c406a5-c6f5-4440-bfba-e8060564b717")
    assert (locs["indx T2"].material, locs["indx T2"].instance_uuid) == ("PETG", None)
    assert (locs["mk4s"].material, locs["mk4s"].instance_uuid) == (None, None)


def test_bridge_config_is_valid_toml_with_the_right_shape():
    cfg = load(EXAMPLE)
    raw = tomllib.loads(bridge_config(cfg))
    core, mk4s, indx = raw["printers"]
    assert core["url"] == "http://127.0.0.1:8001"
    assert core["api_key"] == "sim-core-one-l"
    assert core["tools"] == [{"index": 0, "location": "core-one-l"}]
    assert "uuids" not in core
    assert mk4s["uuids"] is False
    assert indx["tools"][3] == {"index": 3, "location": "indx T4"}


def test_bridge_config_loads_in_the_bridge(tmp_path):
    from spoolman_prusalink.config import load as bridge_load

    cfg = load(EXAMPLE)
    path = tmp_path / "bridge.toml"
    path.write_text('[spoolman]\nurl = "http://127.0.0.1:7912"\n\n' + bridge_config(cfg))
    bridge = bridge_load(path, env={})
    assert [p.name for p in bridge.printers] == ["core-one-l", "mk4s", "indx"]
    assert bridge.printers[0].url == "http://127.0.0.1:8001"
    assert bridge.printers[0].uuids is True
    assert bridge.printers[1].uuids is False
    assert bridge.printers[2].tools == {
        0: "indx T1",
        1: "indx T2",
        2: "indx T3",
        3: "indx T4",
        4: "indx T5",
    }
