"""Errors the Spoolman and PrusaLink clients raise, so callers never see `httpx` or parsing.

Each client sends through `send`, checks the answer with `check`, and parses inside
`parsing`. The bridge catches `ServiceError` and its subclasses only, so an error from
the bridge's own code isn't mistaken for a bad answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

# How much of an error answer's body to keep for the log.
_BODY = 200


class ServiceError(Exception):
    """A request to Spoolman or the printer failed. `str()` says how, for the log.

    `summary` is the same without the body of an error answer. Tell a new failure from
    a repeat by it: a proxy's error page can change on every request, such as with a
    request ID, and that mustn't log the failure again.
    """

    def __init__(self, message: str, summary: str | None = None) -> None:
        super().__init__(message)
        self.summary = message if summary is None else summary


class Unreachable(ServiceError):
    """No answer: the connection was refused or dropped, or the request timed out."""


class BadAnswer(ServiceError):
    """An answer that can't be used: an error status, or a body that can't be read.

    `status` is the HTTP status, or None when the body was the problem. An error that
    may pass, such as a server error, a timeout (408) or a rate limit (429), is a
    `BadAnswer` and not one of its subclasses.
    """

    def __init__(self, message: str, status: int | None = None, summary: str | None = None) -> None:
        super().__init__(message, summary)
        self.status = status


class Rejected(BadAnswer):
    """The service refused the request, and asking again won't change that.

    That's a client error status other than 408 and 429, or a refusal the client
    recognizes in a successful answer.
    """


class NotFound(Rejected):
    """404: what the request names doesn't exist, such as a deleted spool."""


class Busy(Rejected):
    """409: the service can't do it in its current state, such as a printer that's printing."""


async def send(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Send a request. Raises `Unreachable`, or `BadAnswer` for an answer httpx can't read."""
    try:
        return await client.request(method, url, **kwargs)
    except httpx.TransportError as e:
        raise Unreachable(str(e) or type(e).__name__) from e
    except httpx.HTTPError as e:  # Such as a body that can't be decoded.
        raise BadAnswer(str(e) or type(e).__name__) from e


def check(resp: httpx.Response) -> None:
    """Raise the error that fits an answer's status, unless it's a success (2xx)."""
    if resp.is_success:
        return
    code = resp.status_code
    summary = f"{code} {resp.reason_phrase} for {resp.request.method} {resp.request.url.path}"
    body = resp.text.strip()[:_BODY]
    message = f"{summary}: {body}" if body else summary
    if code == 404:
        raise NotFound(message, code, summary)
    if code == 409:
        raise Busy(message, code, summary)
    if resp.is_client_error and code not in (408, 429):
        raise Rejected(message, code, summary)
    raise BadAnswer(message, code, summary)


@contextmanager
def parsing(what: str) -> Iterator[None]:
    """Turn an error parsing `what` into a `BadAnswer`. Keep only parsing inside it."""
    try:
        yield
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise BadAnswer(f"can't read {what}: {type(e).__name__}: {e}") from e
