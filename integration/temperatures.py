"""Filament temperatures shared by the integration fixtures."""

# What the bridge writes for every test filament: the Spoolman temperatures `world` gives
# them, with the preset's preheat. A tag load reports these too, as a tag that agrees with
# Spoolman would.
TEMPERATURES = (220, 170, 60)
WRITTEN = dict(zip(("nozzle", "preheat", "bed"), TEMPERATURES, strict=True))
