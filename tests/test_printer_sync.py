import pytest

from spoolman_prusalink.models import Spool
from spoolman_prusalink.printer_sync import (
    COREONEL_PREAMBLE,
    CannotSync,
    ToolWrite,
    build_job,
    filament_lines,
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
        'M865 X R N"PLA" J"PLA" O"#8c9099" L0',
        'M865.1 I0 C"44c406a5-c6f5-4440-bfba-e8060564b717"',
    ]


def test_spoolman_temps_override_preset_and_alias():
    (line,) = filament_lines(ToolWrite(2, spool(material="TPU", extruder_temp=230,
                                                bed_temp=50, color_hex=None), None))
    assert line == 'M865 X R N"TPU" J"FLEX" T230 B50 L2'


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
    assert job.splitlines()[3:] == ["M117 Spoolman: Core One L+: a, b"]
