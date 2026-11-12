from dataclasses import replace

import pytest

from spoolman_prusalink.models import Material, Spool
from spoolman_prusalink.printer_sync import (
    PRINTER_MODELS,
    CannotSync,
    Temperatures,
    ToolWrite,
    build_job,
    default_preamble,
    filament_lines,
    model_problem,
    printer_matches_write,
    printer_name,
    temperatures,
)

COREONEL_PREAMBLE = default_preamble(PRINTER_MODELS["8.1.0"])


def spool(**kw):
    base = dict(
        id=1,
        location="x",
        name="Polymaker PolySonic™ PLA Grey",
        material="PLA",
        color_hex="8C9099",
        diameter=1.75,
        density=1.23,
    )
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
    (line,) = filament_lines(
        ToolWrite(2, spool(material="TPU", extruder_temp=230, bed_temp=50, color_hex=None), None)
    )
    assert line == 'M865 X R N"TPU" T230 B50 P210 C25 D15 E40 F1 G1 L2'


def test_uuid_is_written_in_canonical_form():
    lines = filament_lines(ToolWrite(0, spool(), "44C406A5-C6F5-4440-BFBA-E8060564B717"))
    assert lines[1] == 'M865.1 I0 C"44c406a5-c6f5-4440-bfba-e8060564b717"'


@pytest.mark.parametrize(
    "value",
    [
        '44c406a5-c6f5-4440-bfba-e8060564b717"\nG1 Z-10\n;',
        "{44c406a5-c6f5-4440-bfba-e8060564b717}",
        "44c406a5c6f54440bfbae8060564b717",
        "44c406a5-c6f5-4440-bfba-e8060564b71",
    ],
)
def test_uuid_that_isnt_canonical_is_refused(value):
    with pytest.raises(CannotSync, match="isn't a UUID"):
        filament_lines(ToolWrite(0, spool(), value))


def test_uuid_only_on_tool_0():
    assert len(filament_lines(ToolWrite(1, spool(), "u"))) == 1


def test_non_preset_needs_temps():
    with pytest.raises(CannotSync):
        filament_lines(ToolWrite(0, spool(material="PLA-CF"), None))


def test_job_ends_with_the_filament_writes():
    write = ToolWrite(1, spool(), None)
    job = build_job(COREONEL_PREAMBLE, [write])
    assert job.splitlines()[3:] == [
        "G28 O ; home only if not homed, so the end-of-job bed move is safe",
        *filament_lines(write),
    ]


def test_non_preset_with_spoolman_temps():
    (line,) = filament_lines(
        ToolWrite(0, spool(material="PLA-CF", extruder_temp=225, bed_temp=65, color_hex=None), None)
    )
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


def test_temperatures():
    assert temperatures(spool()) == Temperatures(215, 170, 60)
    # Preheat never exceeds the nozzle temperature.
    assert temperatures(spool(material="PC", extruder_temp=240)) == Temperatures(240, 240, 100)
    # A material that isn't a preset gets M865 R's default preheat.
    s = spool(material="PLA-CF", extruder_temp=225, bed_temp=65)
    assert temperatures(s) == Temperatures(225, 170, 65)
    with pytest.raises(CannotSync):
        temperatures(spool(material="PLA-CF"))


def reported(name, color, nozzle, preheat, bed):
    return Material(
        name, color, nozzle_temperature=nozzle, preheat_temperature=preheat, bed_temperature=bed
    )


@pytest.mark.parametrize("material", ["PLA", "PLA-CF"])
def test_printer_matches_write_compares_temperatures(material):
    s = spool(material=material, extruder_temp=225, bed_temp=65)
    written = "\n".join(filament_lines(ToolWrite(0, s, None)))
    assert printer_matches_write(reported(material, "#8c9099", 225, 170, 65), written, False)
    assert not printer_matches_write(reported(material, "#8c9099", 230, 170, 65), written, False)
    assert not printer_matches_write(reported(material, "#8c9099", 225, 160, 65), written, False)
    assert not printer_matches_write(reported(material, "#8c9099", 225, 170, 60), written, False)


def test_spool_with_no_material_or_name_cannot_be_written():
    with pytest.raises(CannotSync, match="spool 1 has no material or name"):
        filament_lines(ToolWrite(0, spool(material=None, name="™"), None))


UUID = "44c406a5-c6f5-4440-bfba-e8060564b717"


def test_printer_matches_write_notices_each_difference():
    written = "\n".join(filament_lines(ToolWrite(0, spool(), UUID)))
    ok = Material(
        "PLA",
        "#8c9099",
        nozzle_temperature=215,
        preheat_temperature=170,
        bed_temperature=60,
        instance_uuid=UUID.upper(),  # Case doesn't matter.
    )
    assert printer_matches_write(ok, written, True)
    assert not printer_matches_write(replace(ok, name="PETG"), written, True)
    assert not printer_matches_write(replace(ok, color="#ff0000"), written, True)
    assert not printer_matches_write(replace(ok, instance_uuid=None), written, True)
    other = replace(ok, instance_uuid="9b1d3f6e-0000-4000-8000-000000000000")
    assert not printer_matches_write(other, written, True)
    # With UUIDs off, the printer's UUID isn't compared.
    assert printer_matches_write(other, written, False)
