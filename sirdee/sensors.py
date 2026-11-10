"""Parsing and in-memory state for transducer samples."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SensorState:
    """Runtime state for one sensor."""

    name: str
    range: float = 25.0
    offset: float = 0.0
    enabled: bool = False
    values: list[float] = field(default_factory=list)
    times: list[float] = field(default_factory=list)
    all_values: list[float] = field(default_factory=list)
    all_times: list[float] = field(default_factory=list)
    tared: bool = False
    minimum: Optional[float] = None
    maximum: Optional[float] = None


def parse_sensor_line(line: str, sensor_count: int = 5) -> list[Optional[float]] | None:
    """Parse ``Pot1: value|Pot2: value`` into sensor-indexed values."""
    if not line.strip().startswith("Pot"):
        return None

    values: list[Optional[float]] = [None] * sensor_count
    for part in line.replace("|", ",").split(","):
        if ":" not in part:
            continue
        name, value_text = (item.strip() for item in part.split(":", 1))
        if not name.startswith("Pot"):
            continue
        try:
            index = int(name[3:]) - 1
            value = float(value_text)
        except ValueError:
            continue
        if 0 <= index < sensor_count:
            values[index] = value

    return values if any(value is not None for value in values) else None


def sensor_state(value: float, maximum: float) -> tuple[str, str]:
    """Return the same display state and color used by the monitor."""
    if value < 0 or value > maximum:
        return "Fuera de rango", "#e74c3c"
    margin = maximum * 0.10
    if value < margin or value > maximum - margin:
        return "Advertencia", "#f1c40f"
    return "Normal", "#2ecc71"
