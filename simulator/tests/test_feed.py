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
from simulator.model import MODELS, Invalid, PrintSpec, Queued, Segment, UserJob
from simulator.slicer import INDX_SLICER, XL_SLICER
from simulator.tests.util import PLA, loaded, make

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


def job(*segments: tuple[int, float]) -> UserJob:
    return UserJob(name="job", spec=PrintSpec([Segment(t, mm) for t, mm in segments]))


@pytest.mark.parametrize(("code", "tool"), [("8.1.0", 0), ("3.1.0", 1), ("8.10.0", 1)])
def test_a_print_selects_its_tool_with_no_pick(code, tool):
    sim, p = make(code, [loaded(0), loaded(1)] if tool else None)
    p.selected_tool = None
    p.feed.select_for_print(p, p.tools[tool])
    assert p.selected_tool == tool
    assert not [e for e in sim.events if e.text.startswith("pick ")]


def test_a_toolchanger_primes_each_used_tool_with_its_profile():
    # The XL: tool 0 retracts 2 and primes 39, so 37 count; tool 1 primes 39 and retracts
    # 20. Tool 0 ends picked, and tool 2, unused, moves nothing.
    _, p = make("3.1.0", [loaded(0), loaded(1), loaded(2)])
    p.feed.prime(p, job((0, 10), (1, 10)), XL_SLICER)
    assert [t.extruded / 0.95 for t in p.tools.values()] == pytest.approx([37, 39, 0])
    assert p.selected_tool == 0


@pytest.mark.parametrize("code", ["8.1.0", "3.1.0"])
def test_no_profile_primes_nothing(code):
    # A direct printer, and an XL with one tool, which prints as one.
    _, p = make(code)
    p.feed.prime(p, job((0, 10)), p.profile)
    assert p.tools[0].extruded == 0 and p.tools[0].path.gap == 0


def test_a_toolchanger_s_end_rams_on_a_multi_tool_print_then_parks():
    # The XL: the wipe tower's last ramming, then the 0.8 retract and the park.
    _, p = make("3.1.0", [loaded(0), loaded(1)])
    p.selected_tool = 1
    p.feed.end_moves(p, job((0, 10), (1, 10)), XL_SLICER)
    assert p.tools[1].extruded / 0.95 == pytest.approx(4.1575)
    assert p.tools[1].path.gap == pytest.approx(0.8)
    assert p.tools[0].extruded == 0 and p.selected_tool is None


def test_a_toolchanger_s_one_tool_print_ends_with_no_ramming():
    _, p = make("8.10.0", [loaded(0), loaded(1)])
    p.selected_tool = 1
    p.feed.end_moves(p, job((1, 10)), INDX_SLICER)
    assert p.tools[1].extruded == 0 and p.tools[1].path.gap == pytest.approx(0.8)
    assert p.selected_tool is None


@pytest.mark.parametrize("code", ["8.1.0", "3.1.0"])
def test_no_profile_ends_with_no_moves_and_keeps_the_tool(code):
    _, p = make(code)
    p.feed.end_moves(p, job((0, 10)), p.profile)
    assert p.tools[0].path.gap == 0 and p.selected_tool == 0


@pytest.mark.parametrize(("code", "picked"), [("8.1.0", 0), ("3.1.0", 0), ("8.10.0", None)])
def test_an_unload_empties_the_tool_then_rests_it(code, picked):
    # Only INDX docks after the unload.
    _, p = make(code)
    t = p.tools[0]
    p.feed.end_unload(p, t)
    assert t.unload_count == 1 and t.material is None and not t.path.loaded
    assert p.selected_tool == picked


def test_an_unload_keeps_the_tool_picked_for_a_change_all_s_load():
    _, p = make("8.10.0")
    p.queue = [Queued(0, "load", PLA), Queued(1, "load", PLA)]
    p.feed.end_unload(p, p.tools[0])
    assert p.tools[0].unload_count == 1 and p.selected_tool == 0
    # A queue that goes on with another tool docks this one.
    p.queue = [Queued(1, "load", PLA)]
    p.feed.end_unload(p, p.tools[0])
    assert p.selected_tool is None
