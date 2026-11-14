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
from simulator.slicer import INDX_SLICER, XL_SLICER
from simulator.tests.util import loaded, make

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


@pytest.mark.parametrize(("code", "maps"), [("8.1.0", False), ("3.1.0", True), ("8.10.0", True)])
def test_only_the_toolchangers_map_tools(code, maps):
    _, p = make(code)
    assert p.feed.maps_tools(p) is maps


@pytest.mark.parametrize(
    ("code", "tools", "profile"),
    [
        ("8.1.0", 1, None),
        ("3.1.0", 1, None),  # a one-tool XL prints as a direct printer
        ("3.1.0", 2, XL_SLICER),
        ("8.10.0", 1, INDX_SLICER),  # INDX always docks
    ],
)
def test_the_feed_gives_the_multi_tool_profile_on_a_toolchanger(code, tools, profile):
    _, p = make(code, [loaded(i) for i in range(tools)])
    assert p.feed.profile(p) is profile
    assert p.profile is profile
