"""Color comparison that matches the printer's 5-bit-per-channel storage."""

from __future__ import annotations


def _parse(color: str) -> tuple[int, int, int] | None:
    value = color.strip().lstrip("#")
    if len(value) == 8:  # Spoolman allows RRGGBBAA; the printer has no alpha.
        value = value[:6]
    if len(value) != 6:
        return None
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        return None


def _quantize_channel(value: int) -> int:
    """Round a channel the way the firmware does: keep 5 bits, then expand to 8."""
    five = value >> 3
    return (five << 3) | (five >> 2)


def quantize(color: str) -> str | None:
    """Return `color` as the printer would read it back, or None if it's not a hex color."""
    rgb = _parse(color)
    if rgb is None:
        return None
    return "#" + "".join(f"{_quantize_channel(c):02x}" for c in rgb)


def same_color(a: str | None, b: str | None) -> bool:
    """Compare two hex colors after the printer's rounding. Two missing colors match."""
    if a is None or b is None:
        return a is None and b is None
    qa, qb = quantize(a), quantize(b)
    return qa is not None and qa == qb
