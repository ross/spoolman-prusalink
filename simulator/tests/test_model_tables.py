"""The bridge and the simulator each hold a copy of the firmware's printer model table.

The copies are separate at runtime on purpose: neither imports the other. Only tests
cross the line, and this one keeps the two in step. The aliases sit with the presets in
test_preset_tables.py.
"""

from simulator.model import MODELS
from spoolman_prusalink.printer_sync import _IX, _MULTI_TOOL, _NO_COUNTERS, PRINTER_MODELS


def test_same_models_and_names():
    assert {c: m.name for c, m in PRINTER_MODELS.items()} == {c: m.name for c, m in MODELS.items()}


def test_uuids_agree():
    assert {c: m.uuids for c, m in PRINTER_MODELS.items()} == {
        c: m.uuids for c, m in MODELS.items()
    }


def test_no_counters_matches_the_simulator():
    bridge = {c for c, m in PRINTER_MODELS.items() if m.unsupported == _NO_COUNTERS}
    assert bridge == {c for c, m in MODELS.items() if not m.counters} - multi_tool()


def test_multi_tool_matches_the_simulator():
    assert multi_tool() == {c for c, m in MODELS.items() if m.max_tools > 1}


def test_the_ix_is_the_one_model_refused_for_another_reason():
    other = {
        c: m.unsupported
        for c, m in PRINTER_MODELS.items()
        if m.unsupported not in (None, _NO_COUNTERS, _MULTI_TOOL)
    }
    assert other == {"4.1.0": _IX}


def multi_tool():
    return {c for c, m in PRINTER_MODELS.items() if m.unsupported == _MULTI_TOOL}
