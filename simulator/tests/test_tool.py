"""The extruder profiles the models copy from the firmware."""

from simulator.model import MODELS
from simulator.tool import INDX_EXTRUDER, MINI_EXTRUDER, MK4_EXTRUDER, XL_DEV_KIT_EXTRUDER


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
