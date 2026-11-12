import pytest

from spoolman_prusalink.color import quantize, same_color
from spoolman_prusalink.drift import Difference, What, describe, differences, tag_differences
from spoolman_prusalink.models import Material, Spool

# What the printer reports for the PLA preset, which `spool()` writes.
PLA_TEMPS = {"nozzle_temperature": 215, "preheat_temperature": 170, "bed_temperature": 60}


def spool(**kw):
    base = dict(
        id=1,
        location="Core One L+",
        name="X",
        material="PLA",
        color_hex="73767d",
        diameter=1.75,
        density=1.24,
    )
    base.update(kw)
    return Spool(**base)


def test_quantize_matches_printer_readback():
    # Observed on the printer: #73767d reads back as #73737b.
    assert quantize("#73767d") == "#73737b"
    assert quantize("73767dff") == "#73737b"
    assert quantize("#ffffff") == "#ffffff"
    assert quantize("#000000") == "#000000"
    assert quantize("nope") is None
    assert quantize("zzzzzz") is None  # Six characters, but not hex.


def test_same_color():
    assert same_color("#73767d", "#73737b")
    assert not same_color("#73767d", "#ff0000")
    assert not same_color(None, "#ff0000")
    assert same_color(None, None)
    assert not same_color("zzzzzz", "zzzzzz")  # Colors that can't be read never match.


def test_no_drift_when_matching():
    assert differences(0, Material("PLA", "#73737b", **PLA_TEMPS), spool()) == []


def test_name_compared_case_insensitively_and_truncated():
    assert differences(0, Material("pla", "#73737b", **PLA_TEMPS), spool()) == []
    s = spool(material="PLA Silk Plus")
    assert differences(0, Material("PLASilk", "#73737b", **PLA_TEMPS), s) == []


def test_name_and_color_drift():
    diffs = differences(0, Material("PETG", "#ff0000", **PLA_TEMPS), spool())
    assert describe(diffs) == (
        "name printer='PETG' spoolman='PLA'; color printer=#ff0000 spoolman=#73767d"
    )


def test_uuid_only_checked_on_tool_0():
    s = spool(instance_uuid="44c406a5-c6f5-4440-bfba-e8060564b717")
    m = Material("PLA", "#73737b", None, **PLA_TEMPS)
    assert len(differences(0, m, s)) == 1
    assert differences(1, m, s) == []


def test_mm_to_grams():
    # 1 m of 1.75 mm PLA at 1.24 g/cm³ is about 2.98 g.
    assert round(spool().mm_to_grams(1000), 2) == 2.98


def test_stale_printer_uuid_differs_from_spool_without_one():
    m = Material("PLA", "#73737b", "91da34d9-c1c1-470f-bac7-185a2346f86a", **PLA_TEMPS)
    assert differences(0, m, spool()) == [
        Difference(What.UUID, "91da34d9-c1c1-470f-bac7-185a2346f86a", None)
    ]
    assert describe(differences(0, m, spool())) == (
        "uuid printer=91da34d9-c1c1-470f-bac7-185a2346f86a spoolman=None"
    )
    assert differences(0, m, spool(), uuids=False) == []


def test_temperature_differences():
    tagged = {"nozzle_temperature": 225, "preheat_temperature": 170, "bed_temperature": 60}
    assert differences(0, Material("PLA", "#73737b", **tagged), spool()) == [
        Difference(What.NOZZLE, 225, 215)
    ]
    # Spoolman's temperatures take priority over the preset's.
    assert differences(0, Material("PLA", "#73737b", **tagged), spool(extruder_temp=225)) == []


def test_temperatures_not_compared_for_a_spool_that_cant_be_written():
    # PLA-CF isn't a preset, and with no temperatures in Spoolman it's flagged instead.
    s = spool(material="PLA-CF")
    assert differences(0, Material("PLA-CF", "#73737b", **PLA_TEMPS), s) == []


TAG = "8b6a3e3e-3f6c-4d7b-9a43-6c1f0e7d2a11"


def tag(instance=TAG):
    return Material("PLA", "#73767d", instance, **PLA_TEMPS)


@pytest.mark.parametrize(
    ("material", "active", "others", "want"),
    [
        # Another spool's tag, whether or not the active spool has a UUID.
        (
            tag(),
            spool(id=7),
            [spool(id=8, instance_uuid=TAG.upper())],
            [Difference(What.TAG, TAG, 8)],
        ),
        (
            tag(),
            spool(id=7, instance_uuid="x"),
            [spool(id=8, instance_uuid=TAG)],
            [Difference(What.TAG, TAG, 8)],
        ),
        # The active spool has another UUID.
        (tag(), spool(id=7, instance_uuid="x"), [], [Difference(What.UUID, TAG, "x")]),
        # The spool's own tag, a spool with no UUID, or a tag with none.
        (tag(), spool(id=7, instance_uuid=TAG.upper()), [], []),
        (tag(), spool(id=7), [], []),
        (tag(None), spool(id=7, instance_uuid="x"), [spool(id=8, instance_uuid="y")], []),
    ],
)
def test_tag_differences(material, active, others, want):
    assert tag_differences(material, active, [active, *others]) == want
    # With UUIDs off, nothing about a tag counts.
    assert tag_differences(material, active, [active, *others], uuids=False) == []
