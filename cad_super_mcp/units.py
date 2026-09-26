"""One canonical unit boundary: **millimetres** for lengths, **degrees** for angles.

The model never converts units.  The gateway parses whatever the human said
(``3600``, ``"3.6m"``, ``"12in"``, ``"2'6"``) into millimetres, and converts
to/from the drawing's own units (``INSUNITS``) only at the backend boundary.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .errors import InvalidArgument

# One drawing unit of each INSUNITS code, in millimetres (values cross-checked against
# the Slacker backend's own table, which had once shipped 1000x errors in this area).
MM_PER_UNIT: dict[int, float] = {
    1: 25.4,
    2: 304.8,
    3: 1609344.0,
    4: 1.0,
    5: 10.0,
    6: 1000.0,
    7: 1e6,
    8: 2.54e-5,
    9: 0.0254,
    10: 914.4,
    11: 1e-7,
    12: 1e-6,
    13: 1e-3,
    14: 100.0,
    15: 1e4,
    16: 1e5,
    17: 1e12,
    18: 1.495978707e14,
    19: 9.4607304725808e18,
    20: 3.0856775814913673e19,
}

INSUNITS_NAMES: dict[int, str] = {
    0: "unitless", 1: "inches", 2: "feet", 3: "miles", 4: "millimetres", 5: "centimetres",
    6: "metres", 7: "kilometres", 8: "microinches", 9: "mils", 10: "yards", 11: "angstroms",
    12: "nanometres", 13: "microns", 14: "decimetres", 15: "decametres", 16: "hectometres",
    17: "gigametres", 18: "astronomical_units", 19: "light_years", 20: "parsecs",
}


def units_per_mm(insunits: int | None) -> float | None:
    """Drawing units per millimetre, or ``None`` when INSUNITS is unitless/unknown."""

    if insunits in MM_PER_UNIT:
        return 1.0 / MM_PER_UNIT[int(insunits)]  # type: ignore[arg-type]
    return None


def to_mm(value_du: float, upm: float | None) -> float:
    """Drawing units -> mm.  Unitless drawings pass through 1:1 (as the COM backend does)."""

    return value_du / (upm or 1.0)


def to_drawing_units(value_mm: float, upm: float | None) -> float:
    return value_mm * (upm or 1.0)


# ------------------------------------------------------------------------- parsing
_NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_FEET_INCH = re.compile(
    rf"^\s*({_NUM})\s*(?:'|′|ft|feet|foot)\s*(?:({_NUM})\s*(?:\"|″|in|inch|inches)?)?\s*$",
    re.I,
)
_LENGTH = re.compile(rf"^\s*({_NUM})\s*([^\d\s+\-.]\S*)?\s*$")

_UNIT_MM: dict[str, float] = {
    "mm": 1.0, "millimeter": 1.0, "millimeters": 1.0, "millimetre": 1.0, "millimetres": 1.0,
    "cm": 10.0, "centimeter": 10.0, "centimeters": 10.0, "centimetre": 10.0, "centimetres": 10.0,
    "dm": 100.0, "decimeter": 100.0, "decimeters": 100.0,
    "m": 1000.0, "meter": 1000.0, "meters": 1000.0, "metre": 1000.0, "metres": 1000.0,
    "km": 1e6,
    "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4, "″": 25.4,
    "ft": 304.8, "foot": 304.8, "feet": 304.8, "'": 304.8, "′": 304.8,
    "yd": 914.4, "yard": 914.4, "yards": 914.4,
    "mil": 0.0254,
}

_ACCEPTED = 'a number (millimetres by default) or a string with a unit, e.g. 3600, "3.6m", "360cm", "12in"'


def _finite(x: float, name: str) -> float:
    if not math.isfinite(x):
        raise InvalidArgument(f"{name} must be a finite number (got {x!r})")
    return float(x)


def parse_number(value: Any, name: str = "value") -> float:
    """A plain finite number (angles, ratios).  Numeric strings are accepted."""

    if isinstance(value, bool):
        raise InvalidArgument(f"{name} must be a number, not a boolean")
    if isinstance(value, (int, float)):
        return _finite(float(value), name)
    if isinstance(value, str):
        try:
            return _finite(float(value.strip()), name)
        except ValueError:
            pass
    raise InvalidArgument(f"{name} must be a number (got {value!r})")


def parse_length_mm(value: Any, name: str = "length") -> float:
    """Parse a length into millimetres.  Bare numbers are millimetres."""

    if isinstance(value, bool):
        raise InvalidArgument(f"{name} must be a length, not a boolean")
    if isinstance(value, (int, float)):
        return _finite(float(value), name)
    if isinstance(value, str):
        text = value.strip()
        m = _FEET_INCH.match(text)
        if m:
            feet = float(m.group(1))
            inches = float(m.group(2)) if m.group(2) else 0.0
            sign = -1.0 if feet < 0 else 1.0
            return _finite(sign * (abs(feet) * 304.8 + inches * 25.4), name)
        m = _LENGTH.match(text)
        if m:
            number = float(m.group(1))
            unit = (m.group(2) or "").strip().lower()
            if not unit:
                return _finite(number, name)
            factor = _UNIT_MM.get(unit)
            if factor is not None:
                return _finite(number * factor, name)
            raise InvalidArgument(f"{name}: unknown unit {m.group(2)!r}", hint=f"Accepted: {_ACCEPTED}")
    raise InvalidArgument(f"{name}: cannot parse {value!r}", hint=f"Accepted: {_ACCEPTED}")


def round_mm(x: float, digits: int = 6) -> float:
    r = round(float(x), digits)
    return 0.0 if r == 0 else r  # normalise -0.0


def fmt(x: float, digits: int = 4) -> str:
    """Compact human number: 3600 / 3600.5 / -12.25 (no trailing zeros, no -0)."""

    text = f"{round_mm(x, digits):.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text
