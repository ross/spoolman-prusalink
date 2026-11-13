"""SpoolFields sets up each extra spool field once."""

import httpx

from spoolman_prusalink.fields import SpoolFields
from spoolman_prusalink.spoolman import Spoolman


async def test_a_field_is_set_up_once_then_not_read_again():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=[])

    spoolman = Spoolman("http://spoolman", transport=httpx.MockTransport(handler))
    fields = SpoolFields(spoolman)
    for _ in range(2):
        assert await fields.ensure("bridge_flag", "Printer bridge flag")
    assert requests == [
        ("GET", "/api/v1/field/spool"),
        ("POST", "/api/v1/field/spool/bridge_flag"),
    ]
    await spoolman.aclose()
