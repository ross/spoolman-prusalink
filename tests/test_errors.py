import httpx
import pytest

from spoolman_prusalink.errors import (
    BadAnswer,
    Busy,
    NotFound,
    Rejected,
    Unreachable,
    check,
    parsing,
    send,
)
from spoolman_prusalink.spoolman import Spoolman


async def answer(handler) -> httpx.Response:
    async with httpx.AsyncClient(
        base_url="http://service", transport=httpx.MockTransport(handler)
    ) as client:
        return await send(client, "GET", "/api/thing")


async def test_send_reports_no_answer_as_unreachable():
    def refuse(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(Unreachable, match="refused"):
        await answer(refuse)


async def test_send_reports_an_answer_httpx_cant_read_as_bad():
    def garbled(request):
        raise httpx.DecodingError("bad gzip")

    with pytest.raises(BadAnswer, match="bad gzip") as raised:
        await answer(garbled)
    assert type(raised.value) is BadAnswer
    assert raised.value.status is None


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (404, NotFound),
        (409, Busy),
        (400, Rejected),
        (401, Rejected),
        (408, BadAnswer),
        (429, BadAnswer),
        (500, BadAnswer),
        (503, BadAnswer),
    ],
)
async def test_check_raises_by_status(code, error):
    resp = await answer(lambda request: httpx.Response(code, text=" oops\n"))
    with pytest.raises(error) as raised:
        check(resp)
    assert type(raised.value) is error
    assert raised.value.status == code
    assert str(raised.value) == f"{code} {resp.reason_phrase} for GET /api/thing: oops"


async def test_check_passes_a_success_and_words_an_empty_body_without_it():
    check(await answer(lambda request: httpx.Response(204)))
    resp = await answer(lambda request: httpx.Response(502))
    with pytest.raises(BadAnswer) as raised:
        check(resp)
    assert str(raised.value) == "502 Bad Gateway for GET /api/thing"


def test_parsing_turns_parse_errors_into_bad_answers():
    expected = "can't read the list: KeyError: 'id'"
    with pytest.raises(BadAnswer, match=expected) as raised, parsing("the list"):
        {}["id"]
    assert isinstance(raised.value.__cause__, KeyError)


def test_parsing_lets_other_errors_through():
    with pytest.raises(RuntimeError), parsing("the list"):
        raise RuntimeError("a bug")


@pytest.mark.parametrize("body", ["<html>not json</html>", '{"id": 1}', '[{"id": "x"}]', "[3]"])
async def test_spool_list_that_cant_be_parsed_is_a_bad_answer(body):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    spoolman = Spoolman("http://spoolman", transport=transport)
    with pytest.raises(BadAnswer, match="can't read Spoolman's spool list"):
        await spoolman.spools()
    await spoolman.aclose()
