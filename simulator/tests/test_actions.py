"""The actions the page is told it can take agree with what the model does."""

from __future__ import annotations

import copy
from collections.abc import Callable

import pytest

from simulator.model import Busy, Invalid, PrintSpec, Segment, SimPrinter, SimTool, Simulation
from simulator.tests.test_model import one_segment
from simulator.tests.util import loaded, make, run

PRINTER_CALLS: dict[str, Callable[[SimPrinter], None]] = {
    "start_print": lambda p: p.start_print(one_segment(10)),
    "pause": lambda p: p.pause(),
    "resume": lambda p: p.resume(),
    "cancel": lambda p: p.cancel(),
    "dismiss": lambda p: p.dismiss(),
    "home": lambda p: p.home(),
    "reboot": lambda p: p.reboot(),
    "clear_jam": lambda p: p.clear_jam(),
    "tap_through": lambda p: p.tap_through(),
}
TOOL_CALLS: dict[str, Callable[[SimPrinter, int], None]] = {
    "load": lambda p, i: p.load(i, source="preset", name="PLA"),
    "keep": lambda p, i: p.load(i),
    "unload": lambda p, i: p.unload(i),
    "extrude": lambda p, i: p.extrude(i, 1),
    "retract": lambda p, i: p.retract(i, 1),
    "runout": lambda p, i: p.runout(i),
    "jam": lambda p, i: p.jam(i),
    "select_tool": lambda p, i: p.select_tool(i),
    "set_tool_enabled": lambda p, i: p.set_tool_enabled(i, not p.tools[i].enabled),
}


def idle():
    return make()


def loading():
    sim, p = make(tools=[SimTool(0)])
    p.load(0, source="preset", name="PLA")
    return sim, p


def printing():
    sim, p = make()
    p.start_print(one_segment(100, preheat_s=0))
    run(sim, 1.8)
    return sim, p


def paused():
    sim, p = printing()
    p.pause()
    return sim, p


def runout_wait():
    sim, p = printing()
    p.runout(0)
    return sim, p


def jam_held():
    sim, p = printing()
    p.jam(0, unload=False)
    return sim, p


def model_check():
    sim, p = make()
    p.upload("x.gcode", 'M862.3 P "MK4"\n', print_after=True, overwrite=False)
    return sim, p


def parse_error():
    sim, p = make()
    p.upload("x.gcode", 'M865 L9 N"PLA"\n', print_after=True, overwrite=False)
    run(sim, 1)
    return sim, p


def finished():
    sim, p = make()
    p.start_print(one_segment(5, preheat_s=0))
    run(sim, 10)
    return sim, p


def off():
    sim, p = make()
    p.set_power("off")
    return sim, p


def off_mid_print():
    sim, p = printing()
    p.set_power("off")
    return sim, p


def cold_bed():
    sim, p = printing()
    p.set_power("off")
    p.set_power("on", resume=True, cold_bed=True)
    run(sim, 3)
    return sim, p


def multi_tool_print():
    tools = [loaded(0), loaded(1), SimTool(2), loaded(3, enabled=False)]
    sim, p = make("3.1.0", tools)
    p.start_print(PrintSpec([Segment(0, 100), Segment(1, 100)], preheat_s=0))
    run(sim, 1.8)
    return sim, p


def multi_tool_idle():
    return make("3.1.0", [loaded(0), loaded(1), SimTool(2), loaded(3, enabled=False)])


SITUATIONS = [
    idle,
    loading,
    printing,
    paused,
    runout_wait,
    jam_held,
    model_check,
    parse_error,
    finished,
    off,
    off_mid_print,
    cold_bed,
    multi_tool_print,
    multi_tool_idle,
]


def succeeds(sim: Simulation, call: Callable[[SimPrinter], None]) -> bool:
    """Whether `call` runs on a copy of the printer, which leaves the original as it was."""
    other = copy.deepcopy(sim)
    try:
        call(other.printers["p"])
    except (Busy, Invalid):
        return False
    return True


@pytest.mark.parametrize("situation", SITUATIONS)
def test_published_actions_match_the_model(situation):
    sim, p = situation()
    view = p.to_json()
    assert set(view["actions"]) == set(PRINTER_CALLS)
    for name, reason in view["actions"].items():
        assert succeeds(sim, PRINTER_CALLS[name]) == (reason is None), (name, reason)
    for tool in view["tools"]:
        assert set(tool["actions"]) == set(TOOL_CALLS)
        for name, reason in tool["actions"].items():
            call = TOOL_CALLS[name]
            ok = succeeds(sim, lambda q, call=call, i=tool["index"]: call(q, i))
            assert ok == (reason is None), (tool["index"], name, reason)


def test_a_refused_action_says_why():
    sim, p = printing()
    actions = p.to_json()["actions"]
    assert actions["pause"] is None
    assert actions["dismiss"] == "nothing to dismiss in PRINTING"
    p.set_power("off")
    assert p.to_json()["actions"]["pause"] == "p is off"


def test_the_page_gets_the_setting_fields_and_options():
    sim, p = make()
    view = sim.to_json()
    fields = {f["name"]: f["kind"] for f in view["setting_fields"]}
    assert fields["recover_seconds"] == "number" and fields["start_blip"] == "bool"
    assert view["options"]["power"] == ["on", "off", "hung"]
    assert p.to_json()["load_sources"] == ["tag", "preset"]
    assert make("1.4.1")[1].to_json()["load_sources"] == ["preset"]
