"""Spoolman faults for the integration tests: an outage, or a refused request.

`SpoolmanFaults` sits between the bridge's Spoolman client and the real Spoolman. It
passes every request through until a test asks for a fault: `cut()` makes every
request fail to connect, as a stopped Spoolman would, and `answer()` replies to
matching requests with a status of the test's choosing instead of sending them. A
`hook` runs before each request, to change Spoolman between two of the bridge's
requests or to answer one itself.
`World.spoolman_faults` installs it on the bridge's client the first time a test
asks for it, so worlds that don't ask talk to Spoolman directly.

The rest are changes a user makes in Spoolman's UI that the conftest helper doesn't
cover: deleting, archiving or unarchiving a spool, writing a text field, renaming a location.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from spoolman_prusalink.app import Bridge

Hook = Callable[[httpx.Request], Awaitable[httpx.Response | None]]


@dataclass
class Answer:
    """A canned reply to requests with `method` and `path`, `times` more times (None: all).

    With `containing` set, only to requests whose body contains it.
    """

    method: str
    path: str
    status: int
    body: dict | str | None
    times: int | None
    containing: bytes = b""

    def matches(self, request: httpx.Request) -> bool:
        return (
            request.method == self.method
            and request.url.path == self.path
            and self.containing in request.content
        )

    def response(self, request: httpx.Request) -> httpx.Response:
        body = self.body
        if body is None:
            body = {"detail": f"refused by the test with {self.status}"}
        if isinstance(body, str):
            return httpx.Response(self.status, text=body, request=request)
        return httpx.Response(self.status, json=body, request=request)


class SpoolmanFaults(httpx.AsyncBaseTransport):
    """A transport that passes requests to `inner` unless a test asked for a fault.

    Each request meets, in order: an outage (`cut()`), then `hook`, then the answers
    (`answer()`), then Spoolman. `hook`, when set, is an async callable that gets the
    request before it's answered or sent. It can change Spoolman first, such as move
    or delete a spool between two of the bridge's requests, and return None to carry
    on, or return a response to use instead. Set it back to None to remove it.

    `requests` lists every request the bridge made, as "METHOD /path" with what came
    back: the status code, or "unreachable".
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.down = False
        self.hook: Hook | None = None
        self.answers: list[Answer] = []
        self.requests: list[str] = []

    def cut(self) -> None:
        """Make every request fail with `httpx.ConnectError` until `restore()`."""
        self.down = True

    def restore(self) -> None:
        """End an outage and drop every pending answer and the hook: Spoolman is itself again."""
        self.down = False
        self.answers.clear()
        self.hook = None

    def answer(
        self,
        method: str,
        path: str,
        status: int,
        body: dict | str | None = None,
        *,
        times: int | None = 1,
        containing: bytes = b"",
    ) -> None:
        """Reply `status` to the next `times` requests for `method` `path` (None: until restored).

        `path` is the URL path exactly, such as `/api/v1/spool/7/use`. With `containing`,
        only requests whose body contains those bytes match, such as `b'"location"'` for
        a PATCH that moves a spool. The request isn't sent, so Spoolman doesn't change.
        """
        self.answers.append(Answer(method.upper(), path, status, body, times, containing))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        what = f"{request.method} {request.url.path}"
        if self.down:
            self.requests.append(f"{what} unreachable")
            raise httpx.ConnectError("Spoolman is cut off by the test", request=request)
        if self.hook is not None:
            response = await self.hook(request)
            if response is not None:
                self.requests.append(f"{what} {response.status_code}")
                return response
        for a in self.answers:
            if a.matches(request):
                if a.times is not None:
                    a.times -= 1
                    if a.times == 0:
                        self.answers.remove(a)
                self.requests.append(f"{what} {a.status}")
                return a.response(request)
        response = await self.inner.handle_async_request(request)
        self.requests.append(f"{what} {response.status_code}")
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def install(bridge: Bridge) -> SpoolmanFaults:
    """Put a `SpoolmanFaults` in front of the bridge's Spoolman client, once; return it."""
    client = bridge.spoolman._client
    if not isinstance(client._transport, SpoolmanFaults):
        client._transport = SpoolmanFaults(client._transport)
    return client._transport


async def delete_spool(client: httpx.AsyncClient, spool_id: int) -> None:
    resp = await client.delete(f"/api/v1/spool/{spool_id}")
    resp.raise_for_status()


async def archive_spool(client: httpx.AsyncClient, spool_id: int) -> None:
    resp = await client.patch(f"/api/v1/spool/{spool_id}", json={"archived": True})
    resp.raise_for_status()


async def unarchive_spool(client: httpx.AsyncClient, spool_id: int) -> None:
    resp = await client.patch(f"/api/v1/spool/{spool_id}", json={"archived": False})
    resp.raise_for_status()


async def set_extra_text(client: httpx.AsyncClient, spool_id: int, key: str, text: str) -> None:
    """Write a text extra field, as a user editing the spool in Spoolman would."""
    resp = await client.patch(f"/api/v1/spool/{spool_id}", json={"extra": {key: json.dumps(text)}})
    resp.raise_for_status()


async def rename_location(client: httpx.AsyncClient, old: str, new: str) -> None:
    """Rename a location with Spoolman's rename, which moves every spool in it."""
    resp = await client.patch(f"/api/v1/location/{quote(old, safe='')}", json={"name": new})
    resp.raise_for_status()
