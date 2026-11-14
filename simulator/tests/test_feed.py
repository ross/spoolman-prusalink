"""Each model's feed system and the rules it holds."""

from __future__ import annotations

import pytest

from simulator.feed import FEED_SYSTEMS, Direct, Feed, Toolchanger
from simulator.model import MODELS, Invalid
from simulator.tests.util import make

# The XL, XLP, XL dev kit, Core One INDX and Core One L INDX.
TOOLCHANGERS = {"3.1.0", "3.1.1", "5.1.0", "7.10.0", "8.10.0"}


@pytest.mark.parametrize("code", sorted(MODELS))
def test_model_feed(code):
    expected = Feed.TOOLCHANGER if code in TOOLCHANGERS else Feed.DIRECT
    assert MODELS[code].feed is expected


def test_printer_feed_is_the_models_system():
    _, direct = make("8.1.0")
    _, xl = make("3.1.0")
    assert direct.feed is FEED_SYSTEMS[Feed.DIRECT]
    assert isinstance(direct.feed, Direct)
    assert xl.feed is FEED_SYSTEMS[Feed.TOOLCHANGER]
    assert isinstance(xl.feed, Toolchanger)


def test_direct_refuses_select_tool():
    _, p = make("8.1.0")
    with pytest.raises(Invalid, match="^COREONEL has one tool$"):
        p.select_tool(0)
