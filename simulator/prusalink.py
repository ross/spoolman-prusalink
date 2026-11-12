"""A PrusaLink clone for one simulated printer: the endpoints the bridge calls, and failures.

`serve` answers one request. The aiohttp app from `make_app` calls it for requests that
arrive over a socket, and the httpx transport from `make_transport` calls it in process,
so the routes and the power, key, and fault checks exist once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

import httpx
from aiohttp import web

from simulator.model import Busy, Fault, Power, SimPrinter, Simulation

TRUE_HEADERS = ("?1", "1", "true")
FILES = "/api/v1/files/"


@dataclass(frozen=True)
class Answer:
    """The reply to one request: a status and, unless the reply has no body, its JSON."""

    status: int
    json: dict | None = None


# What a `before_request` hook returns: None to serve the request as usual, an `Answer`
# to send instead, or DROP to close the connection without an answer.
DROP: Literal["drop"] = "drop"
Reply = Answer | Literal["drop"] | None
Hook = Callable[[str, str], Reply]

NOT_FOUND = Answer(404, {"message": "Not Found"})


def serve(
    sim: Simulation,
    name: str,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: str,
    before_request: Hook | None = None,
) -> Answer | Literal["drop"]:
    """Answer one request to printer `name`, or return `DROP` when it gets no answer.

    The printer is looked up per request, so a restore takes effect. A printer that's
    off or hung answers nothing, so that's `DROP` too, and the caller decides how the
    client sees it.

    `before_request(method, path)` runs before the request is served, so a test can
    change the printer between two requests of one poll. It can also answer the request
    itself: an `Answer` it returns is sent instead of the printer's, and `DROP` drops
    the request, as a dropped link would.
    """
    reply = None if before_request is None else before_request(method, path)
    p = sim.printer(name)
    if p.power is not Power.ON or reply == DROP:
        return DROP
    headers = {key.lower(): value for key, value in headers.items()}
    if isinstance(reply, Answer):
        answer = reply
    elif p.fault is Fault.BAD_KEY or headers.get("x-api-key") != p.api_key:
        answer = Answer(401, {"message": "Unauthorized"})
    elif p.fault is Fault.ERROR:
        answer = Answer(500, {"message": "Internal Server Error"})
    else:
        answer = _route(p, method, path, headers, body)
    data = {"method": method, "path": path, "status": answer.status}
    if method == "PUT":
        data["body"] = body
    sim.log(name, "request", f"{method} {path} {answer.status}", data)
    return answer


def _route(p: SimPrinter, method: str, path: str, headers: dict[str, str], body: str) -> Answer:
    if path.startswith(FILES):
        storage, _, file = path.removeprefix(FILES).partition("/")
        if not file:
            return NOT_FOUND
        if method == "PUT":
            return _put_file(p, storage, file, headers, body)
        if method == "POST":
            return _print_file(p, storage, file)
        return Answer(405, {"message": "Method Not Allowed"})
    view = VIEWS.get(path)
    if view is None:
        return NOT_FOUND
    if method != "GET":
        return Answer(405, {"message": "Method Not Allowed"})
    return view(p)


def _job(p: SimPrinter) -> Answer:
    data = p.job_json()
    return Answer(204) if data is None else Answer(200, data)


def _filament(p: SimPrinter) -> Answer:
    data = p.filament_json()
    return NOT_FOUND if data is None else Answer(200, data)


VIEWS: dict[str, Callable[[SimPrinter], Answer]] = {
    "/api/version": lambda p: Answer(200, p.version_json()),
    "/api/v1/status": lambda p: Answer(200, p.status_json()),
    "/api/v1/job": _job,
    "/api/v1/filament": _filament,
}


def _put_file(p: SimPrinter, storage: str, file: str, headers: dict[str, str], body: str) -> Answer:
    if storage != "usb":
        return Answer(403, {"message": "Forbidden"})
    print_after = headers.get("print-after-upload", "").lower() in TRUE_HEADERS
    overwrite = headers.get("overwrite") == "?1"
    try:
        p.upload(file, body, print_after=print_after, overwrite=overwrite)
    except Busy as e:
        return Answer(409, {"message": str(e)})
    info = {
        "name": file,
        "display_name": file,
        "path": f"/usb/{file}",
        "size": len(body.encode()),
        "type": "PRINT_FILE",
    }
    return Answer(201, info)


def _print_file(p: SimPrinter, storage: str, file: str) -> Answer:
    if storage != "usb" or file not in p.files:
        return NOT_FOUND
    try:
        p.print_file(file)
    except Busy as e:
        return Answer(409, {"message": str(e)})
    return Answer(204)


def make_app(sim: Simulation, name: str, before_request: Hook | None = None) -> web.Application:
    """Build the aiohttp app that serves printer `name` over a socket. See `serve`.

    A request that gets no answer hangs while the printer hangs, and then the
    connection closes.
    """

    async def handle(request: web.Request) -> web.StreamResponse:
        body = await request.text()
        answer = serve(
            sim, name, request.method, request.path, request.headers, body, before_request
        )
        if answer == DROP:
            await _no_reply(request, lambda: sim.printer(name))
        if answer.json is None:
            return web.Response(status=answer.status)
        return web.json_response(answer.json, status=answer.status)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    return app


def make_transport(
    sim: Simulation, name: str, before_request: Hook | None = None
) -> httpx.MockTransport:
    """Build an httpx transport that serves printer `name` in process. See `serve`.

    A request that gets no answer raises what httpx raises over a socket: a connect
    error from a printer that's off, a read timeout from one that hangs, and a protocol
    error for a request the hook drops.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        answer = serve(
            sim,
            name,
            request.method,
            request.url.path,
            request.headers,
            request.content.decode(),
            before_request,
        )
        if answer == DROP:
            power = sim.printer(name).power
            if power is Power.OFF:
                raise httpx.ConnectError("All connection attempts failed", request=request)
            if power is Power.HUNG:
                raise httpx.ReadTimeout("", request=request)
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response.", request=request
            )
        if answer.json is None:
            return httpx.Response(answer.status)
        return httpx.Response(answer.status, json=answer.json)

    return httpx.MockTransport(handle)


async def _no_reply(request: web.Request, printer: Callable[[], SimPrinter]) -> None:
    """Hang while the printer hangs, then drop the connection without an answer.

    An off printer's listener is closed, but a kept-alive connection can still carry a
    request, so that is dropped too.
    """
    while printer().power is Power.HUNG:
        await asyncio.sleep(0.2)
    if request.transport is not None:
        request.transport.close()
    raise asyncio.CancelledError
