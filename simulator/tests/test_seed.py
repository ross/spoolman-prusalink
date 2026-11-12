import json
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer

from simulator.config import Location
from simulator.seed import seed


class FakeSpoolman:
    """Just enough of Spoolman API v1 to seed, with the POSTs recorded."""

    def __init__(self):
        self.vendors: list[dict] = []
        self.filaments: list[dict] = []
        self.spools: list[dict] = []
        self.fields: list[dict] = []
        self.posts: list[tuple[str, dict]] = []
        app = web.Application()
        app.add_routes(
            [
                web.get("/api/v1/vendor", self.lister(self.vendors)),
                web.post("/api/v1/vendor", self.creator("vendor", self.vendors)),
                web.get("/api/v1/filament", self.lister(self.filaments)),
                web.post("/api/v1/filament", self.creator("filament", self.filaments)),
                web.get("/api/v1/spool", self.lister(self.spools)),
                web.post("/api/v1/spool", self.creator("spool", self.spools)),
                web.get("/api/v1/field/spool", self.lister(self.fields)),
                web.post("/api/v1/field/spool/{key}", self.create_field),
            ]
        )
        self.app = app

    def lister(self, rows):
        async def handler(request):
            return web.json_response(rows)

        return handler

    def creator(self, kind, rows):
        async def handler(request):
            body = await request.json()
            self.posts.append((kind, body))
            row = {"id": len(rows) + 1, **body}
            if kind == "filament":
                row["vendor"] = {"id": body["vendor_id"]}
            rows.append(row)
            return web.json_response(row)

        return handler

    async def create_field(self, request):
        body = await request.json()
        self.posts.append(("field", body))
        self.fields.append({"key": request.match_info["key"], **body})
        return web.json_response(self.fields)


LOCATIONS = [
    Location("core", 0, "core", True),
    Location("indx", 0, "indx T1", False),
    Location("indx", 1, "indx T2", False),
]


async def test_seed_creates_then_finds(capsys):
    fake = FakeSpoolman()
    async with TestServer(fake.app) as server:
        url = str(server.make_url("")).rstrip("/")
        await seed(url, LOCATIONS)
        first = list(fake.posts)
        await seed(url, LOCATIONS)
    assert fake.posts == first

    assert [v["name"] for v in fake.vendors] == ["Sim"]
    assert [f["name"] for f in fake.filaments] == [
        "PLA",
        "PETG",
        "ASA",
        "Nylon-X",
        "Mystery",
        "PPS",
    ]
    nylon = fake.filaments[3]
    assert (nylon["settings_extruder_temp"], nylon["settings_bed_temp"]) == (260, 90)
    assert "material" not in fake.filaments[4]
    assert fake.fields[0]["key"] == "instance_uuid"

    spools = {s["comment"]: s for s in fake.spools}
    assert set(spools) == {
        "sim: core loaded",
        "sim: core spare",
        "sim: indx T1 loaded",
        "sim: indx T1 spare",
        "sim: indx T2 loaded",
        "sim: indx T2 spare",
    }
    by_id = {f["id"]: f["name"] for f in fake.filaments}
    assert by_id[spools["sim: indx T1 loaded"]["filament_id"]] == "PLA"
    assert by_id[spools["sim: indx T2 loaded"]["filament_id"]] == "PETG"
    assert by_id[spools["sim: indx T2 spare"]["filament_id"]] == "ASA"
    assert spools["sim: indx T2 spare"]["location"] == "shelf"
    assert spools["sim: indx T2 loaded"]["location"] == "indx T2"
    uuid_text = spools["sim: core loaded"]["extra"]["instance_uuid"]
    assert len(json.loads(uuid_text)) == 36
    assert all("extra" not in s for c, s in spools.items() if c != "sim: core loaded")
    assert "found spool" in capsys.readouterr().out


async def test_seed_skips_the_uuid_field_without_a_uuid_printer():
    fake = FakeSpoolman()
    async with TestServer(fake.app) as server:
        await seed(str(server.make_url("")).rstrip("/"), LOCATIONS[1:])
    assert fake.fields == []


async def test_seed_reports_an_unreachable_spoolman():
    import pytest

    from simulator.seed import SeedError

    with pytest.raises(SeedError, match="can't reach Spoolman"):
        await seed("http://127.0.0.1:9", LOCATIONS)


async def test_seed_finds_its_vendor_and_field_among_others():
    fake = FakeSpoolman()
    fake.vendors += [{"id": 1, "name": "Other"}, {"id": 2, "name": "Sim"}]
    fake.fields += [
        {"key": "other", "field_type": "text"},
        {"key": "instance_uuid", "field_type": "text"},
    ]
    async with TestServer(fake.app) as server:
        await seed(str(server.make_url("")).rstrip("/"), LOCATIONS)
    assert [v["name"] for v in fake.vendors] == ["Other", "Sim"]
    assert len(fake.fields) == 2
    assert {f["vendor_id"] for f in fake.filaments} == {2}
    assert "field" not in [kind for kind, _ in fake.posts]


async def test_seed_refuses_a_uuid_field_of_another_type():
    import pytest

    from simulator.seed import SeedError

    fake = FakeSpoolman()
    fake.fields.append({"key": "instance_uuid", "field_type": "integer"})
    async with TestServer(fake.app) as server:
        with pytest.raises(SeedError, match="exists but isn't a text field"):
            await seed(str(server.make_url("")).rstrip("/"), LOCATIONS)
    assert fake.spools == []


async def test_seed_reports_an_error_response_with_the_request():
    import pytest

    from simulator.seed import SeedError

    async def refuse(request):
        return web.Response(status=503, text="down for maintenance")

    app = web.Application()
    app.add_routes([web.get("/api/v1/vendor", refuse)])
    async with TestServer(app) as server:
        url = str(server.make_url("")).rstrip("/")
        with pytest.raises(SeedError) as e:
            await seed(url, LOCATIONS)
    assert str(e.value) == f"GET {url}/api/v1/vendor returned 503: down for maintenance"


async def _seed_one(*locations: Location) -> tuple[dict[str, dict], dict[int, str]]:
    fake = FakeSpoolman()
    async with TestServer(fake.app) as server:
        await seed(str(server.make_url("")).rstrip("/"), list(locations))
    return (
        {s["comment"]: s for s in fake.spools},
        {f["id"]: f["name"] for f in fake.filaments},
    )


async def test_seed_gives_the_tool_its_configured_material_and_uuid():
    configured = "44c406a5-c6f5-4440-bfba-e8060564b717"
    spools, names = await _seed_one(Location("core", 0, "core", True, "PETG", configured))
    loaded = spools["sim: core loaded"]
    assert names[loaded["filament_id"]] == "PETG"
    assert json.loads(loaded["extra"]["instance_uuid"]) == configured
    # The spare follows the cycle from the tool position (PLA, then PETG), not the material.
    assert names[spools["sim: core spare"]["filament_id"]] == "PETG"


async def test_seed_falls_back_to_the_cycle_for_an_unknown_or_missing_material():
    spools, names = await _seed_one(
        Location("a", 1, "a T2", False, "Unobtainium"),
        Location("b", 2, "b T3", False),
    )
    assert names[spools["sim: a T2 loaded"]["filament_id"]] == "PETG"
    assert names[spools["sim: b T3 loaded"]["filament_id"]] == "ASA"


async def test_seed_mints_a_uuid_when_none_is_configured():
    spools, _ = await _seed_one(Location("core", 0, "core", True, "PLA"))
    assert len(json.loads(spools["sim: core loaded"]["extra"]["instance_uuid"])) == 36


async def test_a_seeded_example_tool_agrees_with_the_bridge_drift_check():
    from simulator.config import load, locations
    from spoolman_prusalink import printer_sync
    from spoolman_prusalink.drift import differences
    from spoolman_prusalink.models import Material
    from spoolman_prusalink.spoolman import parse_spool

    cfg = load(Path(__file__).parent.parent / "sim.example.toml")
    locs = locations(cfg)
    fake = FakeSpoolman()
    async with TestServer(fake.app) as server:
        await seed(str(server.make_url("")).rstrip("/"), locs)
    spools = {s["comment"]: s for s in fake.spools}
    configured = {(p.name, t.index): t.material for p in cfg.printers for t in p.tools}
    checked = 0
    for loc in locs:
        material = configured[(loc.printer, loc.tool)]
        if not material:
            continue
        body = spools[f"sim: {loc.location} loaded"]
        filament = next(f for f in fake.filaments if f["id"] == body["filament_id"])
        spool = parse_spool({**body, "id": 1, "filament": filament}, "instance_uuid")
        temps = printer_sync.temperatures(spool)
        have = Material(
            material["name"],
            material["color"],
            material["instance_uuid"],
            nozzle_temperature=temps.nozzle,
            preheat_temperature=temps.preheat,
            bed_temperature=temps.bed,
        )
        assert differences(loc.tool, have, spool, loc.uuids) == []
        checked += 1
    assert checked == 3
