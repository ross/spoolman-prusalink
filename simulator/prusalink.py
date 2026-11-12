"""A PrusaLink clone for one simulated printer: the endpoints the bridge calls, and failures."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal

from aiohttp import web

from simulator.model import Busy, Fault, Power, SimPrinter, Simulation

TRUE_HEADERS = ("?1", "1", "true")

# What a `before_request` hook returns: None to serve the request as usual, a response
# to send instead, or DROP to close the connection without an answer.
DROP: Literal["drop"] = "drop"
Reply = web.StreamResponse | Literal["drop"] | None


def _json(data: dict | None, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def make_app(
    sim: Simulation, name: str, before_request: Callable[[str, str], Reply] | None = None
) -> web.Application:
    """Build the app for printer `name`, looked up per request so a restore takes effect.

    `before_request(method, path)` runs before each request is served, so a test can
    change the printer between two requests of one poll. It can also answer the request
    itself: a response it returns is sent instead of the printer's, and `DROP` closes
    the connection with no answer, as a dropped link would.
    """

    def printer() -> SimPrinter:
        return sim.printer(name)

    @web.middleware
    async def gate(request: web.Request, handler) -> web.StreamResponse:
        reply = None if before_request is None else before_request(request.method, request.path)
        p = printer()
        if p.power is not Power.ON or reply == DROP:
            await _no_reply(request, printer)
        body = None
        if request.method == "PUT":
            body = await request.text()  # aiohttp keeps it, so the handler reads it again
        if isinstance(reply, web.StreamResponse):
            resp: web.StreamResponse = reply
        elif p.fault is Fault.BAD_KEY or request.headers.get("X-Api-Key") != p.api_key:
            resp = _json({"message": "Unauthorized"}, 401)
        elif p.fault is Fault.ERROR:
            resp = _json({"message": "Internal Server Error"}, 500)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as e:
                resp = e
        data = {"body": body} if body is not None else None
        sim.log(name, "request", f"{request.method} {request.path} {resp.status}", data)
        if isinstance(resp, web.HTTPException):
            raise resp
        return resp

    async def version(request: web.Request) -> web.Response:
        return _json(printer().version_json())

    async def status(request: web.Request) -> web.Response:
        return _json(printer().status_json())

    async def job(request: web.Request) -> web.Response:
        data = printer().job_json()
        return web.Response(status=204) if data is None else _json(data)

    async def filament(request: web.Request) -> web.Response:
        data = printer().filament_json()
        return _json({"message": "Not Found"}, 404) if data is None else _json(data)

    async def put_file(request: web.Request) -> web.Response:
        storage, file = request.match_info["storage"], request.match_info["name"]
        if storage != "usb":
            return _json({"message": "Forbidden"}, 403)
        body = await request.text()
        print_after = request.headers.get("Print-After-Upload", "").lower() in TRUE_HEADERS
        overwrite = request.headers.get("Overwrite") == "?1"
        try:
            printer().upload(file, body, print_after=print_after, overwrite=overwrite)
        except Busy as e:
            return _json({"message": str(e)}, 409)
        info = {
            "name": file,
            "display_name": file,
            "path": f"/usb/{file}",
            "size": len(body.encode()),
            "type": "PRINT_FILE",
        }
        return _json(info, 201)

    async def print_file(request: web.Request) -> web.Response:
        p, file = printer(), request.match_info["name"]
        if file not in p.files:
            return _json({"message": "Not Found"}, 404)
        try:
            p.print_file(file)
        except Busy as e:
            return _json({"message": str(e)}, 409)
        return web.Response(status=204)

    app = web.Application(middlewares=[gate])
    app.router.add_get("/api/version", version)
    app.router.add_get("/api/v1/status", status)
    app.router.add_get("/api/v1/job", job)
    app.router.add_get("/api/v1/filament", filament)
    app.router.add_put("/api/v1/files/{storage}/{name:.+}", put_file)
    app.router.add_post("/api/v1/files/usb/{name:.+}", print_file)
    return app


async def _no_reply(request: web.Request, printer) -> None:
    """Hang while the printer hangs, then drop the connection without an answer.

    An off printer's listener is closed, but a kept-alive connection can still carry a
    request, so that is dropped too.
    """
    while printer().power is Power.HUNG:
        await asyncio.sleep(0.2)
    if request.transport is not None:
        request.transport.close()
    raise asyncio.CancelledError
