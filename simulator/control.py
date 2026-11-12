"""The simulator's own API: the page, the state as JSON and SSE, actions, settings, snapshots."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from simulator.model import SNAPSHOT_NAME, Busy, Event, Invalid, PrintSpec, Simulation

STATIC = Path(__file__).parent / "static"
STATE_INTERVAL = 0.1  # at most ten state events a second per client
ADVANCE_LIMIT = 24 * 3600.0  # one jump at most a day, in simulated seconds
HEARTBEAT = 15.0
QUEUE_LIMIT = 1000  # items per SSE client before the oldest are dropped

ACTIONS = frozenset(
    {
        "load",
        "unload",
        "extrude",
        "retract",
        "select_tool",
        "start_print",
        "pause",
        "resume",
        "cancel",
        "dismiss",
        "home",
        "runout",
        "jam",
        "clear_jam",
        "reboot",
        "set_power",
        "set_fault",
        "set_firmware",
        "set_sensor_fault",
        "set_stuck_write",
        "set_tool_enabled",
        "set_auto_accept",
        "counter_step_back",
        "set_extruded_at_unload",
        "tap_through",
    }
)


class Stream:
    """The page's server-sent events: a queue per client of ("log", Event) and ("state", None).

    Every event queues a log item and a state item, since an event comes with a change; so
    does a tick that moved anything.
    """

    def __init__(self, sim: Simulation) -> None:
        self._queues: list[asyncio.Queue] = []
        sim.listeners.append(self._event)
        sim.watchers.append(self.changed)

    def subscribe(self) -> asyncio.Queue:
        """A queue for one client. It's bounded so a client that stops reading costs a fixed
        amount of memory: `push` drops the oldest item to make room."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._queues.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._queues:
            self._queues.remove(queue)

    def push(self, item: tuple) -> None:
        for queue in self._queues:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(item)

    def changed(self) -> None:
        self.push(("state", None))

    def _event(self, event: Event) -> None:
        self.push(("log", event))
        self.changed()


STREAM = web.AppKey("stream", Stream)


def _error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def _body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    # ValueError covers JSONDecodeError and an int too long to convert, which json raises bare.
    except ValueError as e:
        raise Invalid(f"bad JSON: {e}") from None
    if not isinstance(data, dict):
        raise Invalid("the body must be a JSON object")
    return data


def _call(sim: Simulation, name: str, action: str, body: dict) -> None:
    printer = sim.printer(name)
    if action not in ACTIONS:
        raise Invalid(f"unknown action {action!r}")
    if action == "start_print":
        printer.start_print(PrintSpec.from_dict(body))
        return
    method = getattr(printer, action)
    # Check the parameters against the signature before the call, so a TypeError raised
    # inside the action is a bug that answers 500, not a bad request.
    try:
        inspect.signature(method).bind(**body)
    except TypeError as e:
        raise Invalid(f"bad parameters for {action}: {e}") from None
    method(**body)


def make_app(sim: Simulation) -> web.Application:
    stream = Stream(sim)

    async def index(request: web.Request) -> web.StreamResponse:
        page = STATIC / "index.html"
        if not page.is_file():
            return web.Response(text="The page isn't built yet; see /api/state.")
        return web.FileResponse(page)

    async def static(request: web.Request) -> web.StreamResponse:
        root = STATIC.resolve()
        path = (root / request.match_info["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def state(request: web.Request) -> web.Response:
        return web.json_response(sim.to_json())

    async def events(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await resp.prepare(request)
        queue = stream.subscribe()
        try:
            await _send(resp, "state", sim.to_json(events=False))
            while True:
                try:
                    items = [await asyncio.wait_for(queue.get(), HEARTBEAT)]
                except TimeoutError:
                    # A write is the only way to notice a client that went away.
                    await resp.write(b": ping\n\n")
                    continue
                if items[0][0] != "close":
                    await asyncio.sleep(STATE_INTERVAL)
                while not queue.empty():
                    items.append(queue.get_nowait())
                if any(kind == "close" for kind, _ in items):
                    break
                for kind, event in items:
                    if kind == "log":
                        await _send(resp, "log", asdict(event))
                if any(kind == "state" for kind, _ in items):
                    await _send(resp, "state", sim.to_json(events=False))
        except ConnectionResetError:
            pass
        finally:
            stream.unsubscribe(queue)
        return resp

    async def action(request: web.Request) -> web.Response:
        name, act = request.match_info["name"], request.match_info["action"]
        if name not in sim.printers:
            return _error(f"no printer {name!r}", 404)
        try:
            body = await _body(request)
            _call(sim, name, act, body)
        except Busy as e:
            return _error(str(e), 409)
        except Invalid as e:
            return _error(str(e), 400)
        return web.json_response({"ok": True})

    async def settings(request: web.Request) -> web.Response:
        try:
            body = await _body(request)
            sim.settings.update(body)
        except Invalid as e:
            return _error(str(e), 400)
        sim.log(None, "action", "settings " + ", ".join(f"{k}={v}" for k, v in body.items()))
        return web.json_response({"ok": True})

    async def advance(request: web.Request) -> web.Response:
        """Jump simulated time forward, for scripts that drive a scenario step by step."""
        try:
            body = await _body(request)
            seconds = float(body.get("seconds", 0))
        except (Invalid, TypeError, ValueError) as e:
            return _error(f"bad seconds: {e}", 400)
        if not 0 < seconds <= ADVANCE_LIMIT:
            return _error(f"seconds must be in (0, {ADVANCE_LIMIT}]", 400)
        sim.log(None, "action", f"advance {seconds:g} s")
        sim.advance(seconds)
        return web.json_response({"ok": True, "now": sim.now})

    def snapshot_path(request: web.Request) -> Path:
        name = request.match_info["name"]
        if not SNAPSHOT_NAME.match(name):
            raise Invalid(f"snapshot name {name!r} must match {SNAPSHOT_NAME.pattern}")
        if sim.data_dir is None:
            raise Invalid("no data_dir for snapshots")
        return sim.data_dir / f"{name}.json"

    async def save(request: web.Request) -> web.Response:
        try:
            path = snapshot_path(request)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(sim.snapshot(), indent=1))
        except (Invalid, OSError) as e:
            return _error(str(e), 400)
        sim.log(None, "action", f"saved snapshot {path.stem}")
        return web.json_response({"ok": True})

    async def restore(request: web.Request) -> web.Response:
        try:
            path = snapshot_path(request)
            if not path.is_file():
                return _error(f"no snapshot {path.stem!r}", 404)
            sim.restore(json.loads(path.read_text()))
        except (Invalid, OSError, ValueError) as e:
            return _error(str(e), 400)
        return web.json_response({"ok": True})

    async def snapshots(request: web.Request) -> web.Response:
        return web.json_response(sim.snapshot_names())

    async def close_streams(app: web.Application) -> None:
        stream.push(("close", None))

    app = web.Application()
    app[STREAM] = stream
    app.on_shutdown.append(close_streams)
    app.router.add_get("/", index)
    app.router.add_get("/static/{path:.+}", static)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/events", events)
    app.router.add_post("/api/printers/{name}/{action}", action)
    app.router.add_post("/api/settings", settings)
    app.router.add_post("/api/advance", advance)
    app.router.add_post("/api/snapshot/{name}", save)
    app.router.add_post("/api/restore/{name}", restore)
    app.router.add_get("/api/snapshots", snapshots)
    return app


async def _send(resp: web.StreamResponse, event: str, data: dict) -> None:
    await resp.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
