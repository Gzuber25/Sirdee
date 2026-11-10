"""CSV and PDF report generation."""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path
from typing import Mapping

from matplotlib.figure import Figure
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _field(sensor: object, name: str):
    if isinstance(sensor, Mapping):
        return sensor[name]
    return getattr(sensor, name)


def export_csv(path: str | Path, sensors: Mapping[str, object]) -> None:
    enabled = [name for name, sensor in sensors.items() if _field(sensor, "enabled")]
    with open(path, "w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(["Tiempo (s)"] + [name.replace("Pot", "Sensor ") for name in enabled])
        length = max(len(_field(sensors[name], "all_values")) for name in enabled)
        for row_index in range(length):
            first = sensors[enabled[0]]
            if row_index >= len(_field(first, "all_times")):
                continue
            row = [f"{_field(first, 'all_times')[row_index]:.4f}"]
            for name in enabled:
                values = _field(sensors[name], "all_values")
                row.append(f"{values[row_index]:.4f}" if row_index < len(values) else "")
            writer.writerow(row)


def export_pdf(path: str | Path, figure: Figure, sensors: Mapping[str, object]) -> None:
    enabled = [name for name, sensor in sensors.items() if _field(sensor, "all_values")]
    image_path = Path(tempfile.gettempdir()) / "arduino_monitor_chart.png"
    try:
        figure.savefig(image_path, dpi=120, bbox_inches="tight")
        rows = [["Sensor", "Actual", "Min", "Max", "Muestras"]]
        for name in enabled:
            sensor = sensors[name]
            values = _field(sensor, "all_values")
            rows.append([
                name.replace("Pot", "Sensor "),
                f"{values[-1]:.4f}",
                f"{min(values):.4f}",
                f"{max(values):.4f}",
                str(len(values)),
            ])
        document = SimpleDocTemplate(str(path), pagesize=A4)
        table = Table(rows)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#34495e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ]))
        story = [
            Paragraph("Reporte de Transductores", getSampleStyleSheet()["Title"]),
            Spacer(1, 12),
            table,
            Spacer(1, 12),
            Image(str(image_path), width=16 * cm, height=8 * cm),
        ]
        document.build(story)
    finally:
        try:
            image_path.unlink(missing_ok=True)
        except OSError:
            pass
