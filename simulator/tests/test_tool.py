"""The extruder profiles the models copy from the firmware."""

import pytest

from simulator.model import MODELS
from simulator.tool import (
    INDX_EXTRUDER,
    MINI_EXTRUDER,
    MK4_EXTRUDER,
    XL_DEV_KIT_EXTRUDER,
    FilamentPath,
)


def test_feed_ratio():
    assert MK4_EXTRUDER.feed_ratio == 0.95
    assert INDX_EXTRUDER.feed_ratio == 550 / 576
    assert MINI_EXTRUDER.feed_ratio == 1.0


def test_auto_retracts():
    assert MK4_EXTRUDER.auto_retracts
    assert INDX_EXTRUDER.auto_retracts
    assert not XL_DEV_KIT_EXTRUDER.auto_retracts
    assert not MINI_EXTRUDER.auto_retracts


def test_models_stay_hashable():
    assert len(set(MODELS.values())) == len(MODELS)


def test_emptying_a_path_forgets_where_the_filament_was():
    path = FilamentPath(loaded=True, gap=3.0, auto_retracted=55.0)
    path.empty(40.0)
    assert path == FilamentPath(gap=40.0, gap_valid=False)


@pytest.mark.parametrize("filled", [True, False])
def test_filling_a_path_starts_it_at_the_nozzle(filled):
    path = FilamentPath(gap=40.0, gap_valid=False, auto_retracted=55.0)
    path.fill(filled)
    assert path == FilamentPath(loaded=filled)
