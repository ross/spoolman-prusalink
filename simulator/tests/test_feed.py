"""Each model's feed system and the rules it holds."""

from __future__ import annotations

import pytest

from simulator.feed import (
    FEED_SYSTEMS,
    Direct,
    Feed,
    IndxToolchanger,
    Toolchanger,
    XlToolchanger,
)
from simulator.model import MODELS, Invalid
from simulator.tests.util import make

# The XL, XLP and XL dev kit; the Core One INDX and Core One L INDX.
XL_FEED = {"3.1.0", "3.1.1", "5.1.0"}
INDX_FEED = {"7.10.0", "8.10.0"}


@pytest.mark.parametrize("code", sorted(MODELS))
def test_model_feed(code):
    expected = Feed.XL if code in XL_FEED else Feed.INDX if code in INDX_FEED else Feed.DIRECT
    assert MODELS[code].feed is expected
    # Only the toolchangers have more than one tool.
    assert (MODELS[code].max_tools > 1) == (expected is not Feed.DIRECT)


@pytest.mark.parametrize(
    ("code", "feed", "system"),
    [
        ("8.1.0", Feed.DIRECT, Direct),
        ("3.1.0", Feed.XL, XlToolchanger),
        ("8.10.0", Feed.INDX, IndxToolchanger),
    ],
)
def test_printer_feed_is_the_models_system(code, feed, system):
    _, p = make(code)
    assert p.feed is FEED_SYSTEMS[feed]
    assert type(p.feed) is system
    assert isinstance(p.feed, Toolchanger) == (feed is not Feed.DIRECT)


def test_direct_refuses_select_tool():
    _, p = make("8.1.0")
    with pytest.raises(Invalid, match="^COREONEL has one tool$"):
        p.select_tool(0)
