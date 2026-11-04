from spoolman_prusalink.color import quantize, same_color
from spoolman_prusalink.drift import differences
from spoolman_prusalink.models import Material, Spool


def spool(**kw):
    base = dict(id=1, location="Core One L+", name="X", material="PLA", color_hex="73767d",
                diameter=1.75, density=1.24)
    base.update(kw)
    return Spool(**base)


def test_quantize_matches_printer_readback():
    # Observed on the printer: #73767d reads back as #73737b.
    assert quantize("#73767d") == "#73737b"
    assert quantize("73767dff") == "#73737b"
    assert quantize("#ffffff") == "#ffffff"
    assert quantize("#000000") == "#000000"
    assert quantize("nope") is None


def test_same_color():
    assert same_color("#73767d", "#73737b")
    assert not same_color("#73767d", "#ff0000")
    assert not same_color(None, "#ff0000")
    assert same_color(None, None)


def test_no_drift_when_matching():
    assert differences(0, Material("PLA", "#73737b"), spool()) == []


def test_name_compared_case_insensitively_and_truncated():
    assert differences(0, Material("pla", "#73737b"), spool()) == []
    s = spool(material="PLA Silk Plus")
    assert differences(0, Material("PLA Sil", "#73737b"), s) == []


def test_name_and_color_drift():
    diffs = differences(0, Material("PETG", "#ff0000"), spool())
    assert len(diffs) == 2


def test_uuid_only_checked_on_tool_0():
    s = spool(instance_uuid="44c406a5-c6f5-4440-bfba-e8060564b717")
    m = Material("PLA", "#73737b", None)
    assert len(differences(0, m, s)) == 1
    assert differences(1, m, s) == []


def test_mm_to_grams():
    # 1 m of 1.75 mm PLA at 1.24 g/cm³ is about 2.98 g.
    assert round(spool().mm_to_grams(1000), 2) == 2.98
    assert spool(density=None).mm_to_grams(1000) is None
