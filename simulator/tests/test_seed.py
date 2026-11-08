import json

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
