import pytest

from spoolman_prusalink.models import Spool
from spoolman_prusalink.printer_sync import (
    COREONEL_PREAMBLE,
    PRINTER_MODELS,
    CannotSync,
    ToolWrite,
    build_job,
    default_preamble,
    filament_lines,
    model_problem,
    printer_name,
)


def spool(**kw):
    base = dict(id=1, location="x", name="Polymaker PolySonic™ PLA Grey", material="PLA",
                color_hex="8C9099", diameter=1.75, density=1.23)
    base.update(kw)
    return Spool(**base)


def test_name_uses_firmware_charset():
    assert printer_name(spool(material="PLA+")) == "PLA"
    assert printer_name(spool(material="PETG Carbon")) == "PETGCar"
    assert printer_name(spool(material="", name="My Spool")) == "MySpool"
    assert printer_name(spool(material=None, name="™")) is None


def test_preset_with_color_and_uuid():
    lines = filament_lines(ToolWrite(0, spool(), "44c406a5-c6f5-4440-bfba-e8060564b717"))
    assert lines == [
        'M865 X R N"PLA" T215 B60 P170 C20 D15 E38 O"#8c9099" L0',
        'M865.1 I0 C"44c406a5-c6f5-4440-bfba-e8060564b717"',
    ]


def test_spoolman_temps_override_preset_and_alias():
    (line,) = filament_lines(ToolWrite(2, spool(material="TPU", extruder_temp=230,
                                                bed_temp=50, color_hex=None), None))
    assert line == 'M865 X R N"TPU" T230 B50 P210 C25 D15 E40 F1 G1 L2'


def test_uuid_only_on_tool_0():
    assert len(filament_lines(ToolWrite(1, spool(), "u"))) == 1


def test_non_preset_needs_temps():
    with pytest.raises(CannotSync):
        filament_lines(ToolWrite(0, spool(material="PLA-CF"), None))


def test_job_message_is_ascii_and_multi_tool_uses_screen_numbers():
    job = build_job(COREONEL_PREAMBLE, [ToolWrite(1, spool(), None)], [], multi_tool=True)
    assert job.splitlines()[-1] == "M117 Spoolman: T2 Polymaker PolySonic PLA Gre"
    assert ";" not in job.splitlines()[-1]


def test_notice_only_job():
    job = build_job(COREONEL_PREAMBLE, [], ["Core One L+: a; b"], multi_tool=False)
    assert job.splitlines()[3:] == [
        "G28 O ; home only if not homed, so the end-of-job bed move is safe",
        "M117 Spoolman: Core One L+: a, b",
    ]


def test_non_preset_with_spoolman_temps():
    (line,) = filament_lines(ToolWrite(0, spool(material="PLA-CF", extruder_temp=225,
                                                bed_temp=65, color_hex=None), None))
    assert line == 'M865 X R N"PLA-CF" T225 B65 L0'


@pytest.mark.parametrize("material", ["PPS", "PPA", " pps "])
def test_ht_idler_door_materials_need_menu_load(material):
    # With temperatures set, these would otherwise pass as a non-preset material.
    s = spool(material=material, extruder_temp=320, bed_temp=105)
    with pytest.raises(CannotSync, match="HT idler-door"):
        filament_lines(ToolWrite(0, s, None))


def test_model_problems():
    assert model_problem("8.1.0", None) is None
    assert model_problem("1.4.1", None) is None
    assert model_problem("1.4.1", ['M862.3 P "MK4SMMU3"']) is None
    assert "is for COREONEL but the printer is MK4" in model_problem("1.4.0", COREONEL_PREAMBLE)
    assert "MINI isn't supported" in model_problem("2.1.0", None)
    assert "multi-tool" in model_problem("8.10.0", None)
    assert "isn't one the bridge knows" in model_problem("99.0.0", None)
    assert "no M862.3" in model_problem("1.4.0", ["M862.5 P2"])


def test_default_preamble_names_model():
    assert default_preamble(PRINTER_MODELS["1.4.0"])[0].startswith('M862.3 P "MK4"')
    assert COREONEL_PREAMBLE[0] == 'M862.3 P "COREONEL" ; printer model check'
