"""Fill a Spoolman with the simulator's vendor, filaments and spools. Safe to run twice."""

from __future__ import annotations

import json
import uuid

import aiohttp

from simulator.config import Location

VENDOR = "Sim"
SHELF = "shelf"
# Materials cycled over the tools, in order.
CYCLE = ("PLA", "PETG", "ASA")

# name -> the filament's Spoolman fields (besides name and vendor).
FILAMENTS: dict[str, dict] = {
    "PLA": {"material": "PLA", "color_hex": "73737b", "density": 1.24},
    "PETG": {"material": "PETG", "color_hex": "1f6fb2", "density": 1.27},
    "ASA": {"material": "ASA", "color_hex": "d9342b", "density": 1.07},
    # A type the printer has no preset for, so it takes the ad-hoc path.
    "Nylon-X": {
        "material": "NYLONX",
        "color_hex": "c9a227",
        "density": 1.14,
        "settings_extruder_temp": 260,
        "settings_bed_temp": 90,
    },
    # No material and no temperatures: nothing to write to the printer.
    "Mystery": {"color_hex": "8a2be2", "density": 1.24},
    "PPS": {"material": "PPS", "color_hex": "333333", "density": 1.35},
}


class SeedError(RuntimeError):
    pass


class _Api:
    def __init__(self, session: aiohttp.ClientSession, base: str):
        self._session = session
        self._base = base.rstrip("/")

    async def request(self, method: str, path: str, **kwargs) -> object:
        url = f"{self._base}{path}"
        try:
            async with self._session.request(method, url, **kwargs) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise SeedError(f"{method} {url} returned {resp.status}: {body[:200]}")
                return json.loads(body) if body else None
        except aiohttp.ClientConnectionError as e:
            raise SeedError(
                f"can't reach Spoolman at {self._base} ({e}); is it running? "
                "Locally it is at http://127.0.0.1:7912"
            ) from e


async def _vendor(api: _Api) -> int:
    for vendor in await api.request("GET", "/api/v1/vendor"):
        if vendor["name"] == VENDOR:
            print(f"found vendor {VENDOR} (id {vendor['id']})")
            return vendor["id"]
    vendor = await api.request("POST", "/api/v1/vendor", json={"name": VENDOR})
    print(f"created vendor {VENDOR} (id {vendor['id']})")
    return vendor["id"]


async def _filaments(api: _Api, vendor_id: int) -> dict[str, int]:
    existing = {
        f["name"]: f["id"]
        for f in await api.request("GET", "/api/v1/filament")
        if (f.get("vendor") or {}).get("id") == vendor_id
    }
    ids: dict[str, int] = {}
    for name, fields in FILAMENTS.items():
        if name in existing:
            ids[name] = existing[name]
            print(f"found filament {name} (id {ids[name]})")
            continue
        body = {"name": name, "vendor_id": vendor_id, "diameter": 1.75, "weight": 1000, **fields}
        created = await api.request("POST", "/api/v1/filament", json=body)
        ids[name] = created["id"]
        print(f"created filament {name} (id {ids[name]})")
    return ids


async def _ensure_field(api: _Api, key: str) -> None:
    for existing in await api.request("GET", "/api/v1/field/spool"):
        if existing["key"] == key:
            if existing["field_type"] != "text":
                raise SeedError(f"spool extra field {key!r} exists but isn't a text field")
            return
    await api.request(
        "POST", f"/api/v1/field/spool/{key}", json={"name": key, "field_type": "text"}
    )
    print(f"created spool extra field {key}")


async def seed(
    spoolman_url: str, locations: list[Location], *, uuid_field: str = "instance_uuid"
) -> None:
    """Create what is missing; never delete or move anything.

    A spool's `comment` is its identity, so a spool the user moved or edited is still found.
    """
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        api = _Api(session, spoolman_url)
        vendor_id = await _vendor(api)
        filaments = await _filaments(api, vendor_id)
        if any(loc.uuids and loc.tool == 0 for loc in locations):
            await _ensure_field(api, uuid_field)
        comments = {s.get("comment") for s in await api.request("GET", "/api/v1/spool")}

        async def spool(comment: str, material: str, location: str, extra: dict | None) -> None:
            if comment in comments:
                print(f"found spool {comment!r}")
                return
            body: dict = {
                "filament_id": filaments[material],
                "location": location,
                "comment": comment,
            }
            if extra:
                body["extra"] = extra
            created = await api.request("POST", "/api/v1/spool", json=body)
            comments.add(comment)
            print(f"created spool {comment!r} (id {created['id']}, {material} in {location})")

        for loc in locations:
            position = loc.tool % len(CYCLE)
            # The tool's configured material and UUID, so the bridge starts in agreement
            # with the printer. A material seed has no filament for falls back to the cycle.
            loaded = loc.material if loc.material in FILAMENTS else CYCLE[position]
            extra = None
            if loc.uuids and loc.tool == 0:
                # Minted only when the spool is created, so a rerun keeps the UUID.
                value = loc.instance_uuid or str(uuid.uuid4())
                extra = {uuid_field: json.dumps(value)}
            await spool(f"sim: {loc.location} loaded", loaded, loc.location, extra)
            spare = CYCLE[(position + 1) % len(CYCLE)]
            await spool(f"sim: {loc.location} spare", spare, SHELF, None)
