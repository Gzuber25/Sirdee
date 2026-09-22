"""
Monitor de Transductores Arduino
--------------------------------
Aplicación de escritorio en un solo archivo.

Requisitos:
    pip install customtkinter pyserial matplotlib pandas reportlab

Formato esperado del Arduino (una línea por muestra):
    25.43,24.91,25.12,25.30,24.88\n

Cambios principales:
    - La aplicación inicia en pantalla completa.
    - F11 permite activar/desactivar pantalla completa.
    - Esc permite salir de pantalla completa.
    - Interfaz escalada para mejorar la lectura.
    - Tema predeterminado: claro.
    - Botón Claro/Oscuro funcional.
    - La gráfica también cambia entre tema claro y oscuro.
    - Ventanas de configuración/calibración respetan el tema actual.
"""

import json
import os
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from queue import Empty, Queue
from tkinter import filedialog, messagebox
from typing import Callable, Dict, List, Optional, Tuple

import customtkinter as ctk
import pandas as pd
import serial
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from serial.tools import list_ports


# =============================================================
# CONSTANTES
# =============================================================

CONFIG_FILE = "config.json"
REFRESH_MS = 100
NUM_SENSORS = 5

# Escala general de la interfaz.
# 1.0 = normal
# 1.15 = ligeramente grande
# 1.25 = recomendada para mejor lectura
UI_SCALE = 1.25

SENSOR_COLORS = [
    "#1f6aa5",
    "#0e7c7b",
    "#8e44ad",
    "#c0392b",
    "#d68910",
]

STATE_INFO = {
    "normal": ("🟢 Normal", "#2ecc71"),
    "warning": ("🟡 Advertencia", "#f1c40f"),
    "out": ("🔴 Fuera de rango", "#e74c3c"),
    "none": ("⚪ Sin datos", "#95a5a6"),
}


# =============================================================
# COLORES DE TEMA
# =============================================================

LIGHT_THEME = {
    "window": "#f2f4f7",
    "header": "#ffffff",
    "toolbar": "#e9edf2",
    "footer": "#ffffff",
    "panel": "#ffffff",
    "card": "#ffffff",
    "graph": "#ffffff",
    "text": "#1f2937",
    "secondary_text": "#5b6470",
    "border": "#d5dbe3",
    "button": "#2f6fad",
    "button_hover": "#3f82c1",
    "secondary_button": "#dce3eb",
    "secondary_button_hover": "#cbd5df",
    "danger": "#c0392b",
    "danger_hover": "#e74c3c",
    "success": "#27ae60",
    "success_hover": "#2ecc71",
    "purple": "#8e44ad",
    "purple_hover": "#9b59b6",
    "teal": "#16a085",
    "teal_hover": "#1abc9c",
    "plot_text": "#4b5563",
    "plot_grid": "#cfd6df",
    "plot_border": "#b8c1cc",
}

DARK_THEME = {
    "window": "#0e1116",
    "header": "#151a21",
    "toolbar": "#12161d",
    "footer": "#151a21",
    "panel": "#151a21",
    "card": "#1c1f26",
    "graph": "#151a21",
    "text": "#ecf0f1",
    "secondary_text": "#95a5a6",
    "border": "#2c3e50",
    "button": "#1f6aa5",
    "button_hover": "#2980b9",
    "secondary_button": "#2c3e50",
    "secondary_button_hover": "#34495e",
    "danger": "#c0392b",
    "danger_hover": "#e74c3c",
    "success": "#27ae60",
    "success_hover": "#2ecc71",
    "purple": "#8e44ad",
    "purple_hover": "#9b59b6",
    "teal": "#16a085",
    "teal_hover": "#1abc9c",
    "plot_text": "#95a5a6",
    "plot_grid": "#2c3e50",
    "plot_border": "#2c3e50",
}


# =============================================================
# CONFIGURACIÓN
# =============================================================

@dataclass
class SensorConfig:
    name: str
    unit: str = "mm"
    min_value: float = 0.0
    max_value: float = 50.0
    warning_margin: float = 0.10
    offset: float = 0.0


@dataclass
class AppConfig:
    port: str = ""
    baudrate: int = 9600
    read_frequency: int = 10
    history_size: int = 300
    sensors: List[SensorConfig] = field(
        default_factory=lambda: [
            SensorConfig(name=f"Sensor {i + 1}")
            for i in range(NUM_SENSORS)
        ]
    )

    def save(self, path: str = CONFIG_FILE) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str = CONFIG_FILE) -> "AppConfig":
        if not os.path.exists(path):
            return cls()

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            sensors_data = data.get("sensors", [])

            if sensors_data:
                data["sensors"] = [
                    SensorConfig(**sensor)
                    for sensor in sensors_data
                ]
            else:
                data["sensors"] = [
                    SensorConfig(name=f"Sensor {i + 1}")
                    for i in range(NUM_SENSORS)
                ]

            return cls(**data)

        except Exception as exc:
            print(f"[Config] Error al cargar, usando por defecto: {exc}")
            return cls()


# =============================================================
# SERIAL MANAGER
# =============================================================

class SerialManager:
    """Lectura serial en hilo separado con cola no bloqueante."""

    def __init__(self, num_sensors: int = NUM_SENSORS):
        self.num_sensors = num_sensors
        self._serial: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._data_queue: "Queue[Tuple[List[Optional[float]], bool]]" = Queue(maxsize=200)
        self._connected = False
        self._on_disconnect: Optional[Callable[[], None]] = None

    @staticmethod
    def list_ports() -> List[str]:
        return [p.device for p in list_ports.comports()]

    @property
    def is_connected(self) -> bool:
        return (
            self._connected
            and self._serial is not None
            and self._serial.is_open
        )

    def connect(self, port: str, baudrate: int = 9600) -> bool:
        try:
            self._serial = serial.Serial(
                port,
                baudrate,
                timeout=1
            )

            time.sleep(0.5)
            self._serial.reset_input_buffer()

            self._connected = True
            self._stop_event.clear()

            self._thread = threading.Thread(
                target=self._read_loop,
                daemon=True
            )
            self._thread.start()

            return True

        except serial.SerialException as exc:
            print(f"[Serial] Error al conectar: {exc}")
            self._serial = None
            self._connected = False
            return False

    def disconnect(self) -> None:
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)

        self._thread = None

        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass

        self._serial = None
        self._connected = False

    def read_data(self) -> Optional[List[float]]:
        try:
            values, _ = self._data_queue.get_nowait()
            return values
        except Empty:
            return None

    def read_data_with_format(self) -> Optional[Tuple[List[Optional[float]], bool]]:
        """Returns values and whether they came from the PotN firmware format."""
        try:
            return self._data_queue.get_nowait()
        except Empty:
            return None

    def send_command(self, command: str) -> bool:
        if not self.is_connected or self._serial is None:
            return False

        try:
            self._serial.write(f"{command.rstrip(chr(10))}\n".encode("utf-8"))
            return True
        except serial.SerialException as exc:
            print(f"[Serial] Error enviando comando: {exc}")
            return False

    def set_disconnect_callback(
        self,
        callback: Callable[[], None]
    ) -> None:
        self._on_disconnect = callback

    def _read_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if (
                    self._serial is None
                    or not self._serial.is_open
                ):
                    break

                line = (
                    self._serial
                    .readline()
                    .decode("utf-8", errors="ignore")
                    .strip()
                )

                if not line:
                    continue

                parsed = self._parse_line(line)

                if parsed is None:
                    continue

                values, is_pot_format = parsed

                if self._data_queue.full():
                    try:
                        self._data_queue.get_nowait()
                    except Empty:
                        pass

                self._data_queue.put_nowait((values, is_pot_format))

            except serial.SerialException as exc:
                print(f"[Serial] Conexión perdida: {exc}")
                break

            except Exception as exc:
                print(f"[Serial] Error: {exc}")
                time.sleep(0.05)

        was_connected = self._connected
        self._connected = False

        if was_connected and self._on_disconnect:
            try:
                self._on_disconnect()
            except Exception:
                pass

    def _parse_line(
        self,
        line: str
    ) -> Optional[Tuple[List[Optional[float]], bool]]:
        try:
            if ":" in line:
                values = [None] * self.num_sensors
                for part in line.replace("|", ",").split(","):
                    name, value = part.split(":", 1)
                    index = int(name.strip().replace("Pot", "")) - 1
                    if 0 <= index < self.num_sensors:
                        values[index] = float(value.strip())

                if all(value is None for value in values):
                    return None

                return values, True

            parts = line.replace(";", ",").split(",")

            if len(parts) < self.num_sensors:
                return None

            return [float(parts[i]) for i in range(self.num_sensors)], False

        except (ValueError, IndexError):
            return None


# =============================================================
# DATA PROCESSOR
# =============================================================

class DataProcessor:
    """Aplica offset, evalúa estado y almacena historial limitado."""

    def __init__(self, config: AppConfig):
        self.config = config

        self.history: Dict[int, deque] = {
            i: deque(maxlen=config.history_size)
            for i in range(len(config.sensors))
        }

        self.timestamps: deque = deque(
            maxlen=config.history_size
        )

    def update_history_limit(self, size: int) -> None:
        self.config.history_size = size

        for i in range(len(self.config.sensors)):
            self.history[i] = deque(
                self.history[i],
                maxlen=size
            )

        self.timestamps = deque(
            self.timestamps,
            maxlen=size
        )

    def process(
        self,
        raw_values: List[Optional[float]],
        timestamp: float,
        invert_range: bool = False
    ) -> List[Dict]:

        results = []

        self.timestamps.append(timestamp)

        for idx, raw in enumerate(raw_values):
            if raw is None:
                continue

            cfg = self.config.sensors[idx]

            value = (
                cfg.max_value - raw
                if invert_range
                else raw
            ) + cfg.offset
            state = self._evaluate_state(value, cfg)

            self.history[idx].append(value)

            results.append({
                "index": idx,
                "name": cfg.name,
                "unit": cfg.unit,
                "value": value,
                "state": state,
            })

        return results

    @staticmethod
    def _evaluate_state(
        value: float,
        cfg: SensorConfig
    ) -> str:

        if value < cfg.min_value or value > cfg.max_value:
            return "out"

        margin = (
            cfg.max_value - cfg.min_value
        ) * cfg.warning_margin

        if margin <= 0:
            return "normal"

        if (
            value < cfg.min_value + margin
            or value > cfg.max_value - margin
        ):
            return "warning"

        return "normal"

    def tare(
        self,
        sensor_index: int,
        current_value: float
    ) -> None:
        self.config.sensors[sensor_index].offset = -current_value

    def set_offset(
        self,
        sensor_index: int,
        offset: float
    ) -> None:
        self.config.sensors[sensor_index].offset = offset

    def clear(self) -> None:
        for d in self.history.values():
            d.clear()

        self.timestamps.clear()


# =============================================================
# EXPORTACIÓN CSV / PDF
# =============================================================

class DataExporter:

    def __init__(
        self,
        processor: DataProcessor,
        config: AppConfig
    ):
        self.processor = processor
        self.config = config

    def to_dataframe(self) -> pd.DataFrame:
        data = {
            "Tiempo (s)": list(self.processor.timestamps)
        }

        n = len(self.processor.timestamps)

        for idx, values in self.processor.history.items():
            name = self.config.sensors[idx].name
            unit = self.config.sensors[idx].unit

            col = f"{name} ({unit})"

            vals = list(values)

            if len(vals) < n:
                vals = [None] * (n - len(vals)) + vals

            data[col] = vals[-n:] if n else []

        return pd.DataFrame(data)

    def save_csv(self, path: str) -> None:
        self.to_dataframe().to_csv(
            path,
            index=False
        )

    def save_pdf(
        self,
        path: str,
        chart_image_path: Optional[str] = None
    ) -> None:

        doc = SimpleDocTemplate(
            path,
            pagesize=A4,
            leftMargin=2 * cm,
            rightMargin=2 * cm,
            topMargin=2 * cm,
            bottomMargin=2 * cm,
        )

        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            "TitleStyle",
            parent=styles["Title"],
            textColor=colors.HexColor("#1f6aa5"),
        )

        story = [
            Paragraph(
                "Reporte de Monitoreo – Transductores",
                title_style
            ),
            Paragraph(
                f"Generado: "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                styles["Normal"]
            ),
            Spacer(1, 0.6 * cm),
        ]

        df = self.to_dataframe()

        summary = [
            [
                "Sensor",
                "Mín",
                "Máx",
                "Promedio",
                "Muestras"
            ]
        ]

        for idx in range(len(self.config.sensors)):
            col = (
                f"{self.config.sensors[idx].name} "
                f"({self.config.sensors[idx].unit})"
            )

            if col in df.columns:
                serie = df[col].dropna()

                summary.append([
                    self.config.sensors[idx].name,
                    f"{serie.min():.3f}"
                    if len(serie) else "-",
                    f"{serie.max():.3f}"
                    if len(serie) else "-",
                    f"{serie.mean():.3f}"
                    if len(serie) else "-",
                    str(len(serie)),
                ])

        table = Table(
            summary,
            hAlign="LEFT"
        )

        table.setStyle(
            TableStyle([
                (
                    "BACKGROUND",
                    (0, 0),
                    (-1, 0),
                    colors.HexColor("#1f6aa5")
                ),
                (
                    "TEXTCOLOR",
                    (0, 0),
                    (-1, 0),
                    colors.white
                ),
                (
                    "GRID",
                    (0, 0),
                    (-1, -1),
                    0.5,
                    colors.grey
                ),
                (
                    "FONTNAME",
                    (0, 0),
                    (-1, 0),
                    "Helvetica-Bold"
                ),
                (
                    "ALIGN",
                    (1, 0),
                    (-1, -1),
                    "CENTER"
                ),
            ])
        )

        story.append(table)

        if (
            chart_image_path
            and os.path.exists(chart_image_path)
        ):
            story.append(
                Spacer(1, 0.8 * cm)
            )

            story.append(
                Paragraph(
                    "Gráfica de tendencia",
                    styles["Heading2"]
                )
            )

            story.append(
                Spacer(1, 0.3 * cm)
            )

            try:
                story.append(
                    Image(
                        chart_image_path,
                        width=16 * cm,
                        height=8 * cm
                    )
                )
            except Exception as exc:
                print(
                    f"[PDF] No se pudo insertar gráfica: {exc}"
                )

        doc.build(story)


# =============================================================
# TARJETA DE SENSOR
# =============================================================

class SensorCard(ctk.CTkFrame):

    def __init__(
        self,
        master,
        index: int,
        name: str,
        unit: str,
        on_tare: Callable[[int], None],
        on_toggle: Callable[[int, bool], None],
        theme: Dict[str, str],
        **kwargs
    ):
        super().__init__(
            master,
            corner_radius=14,
            fg_color=theme["card"],
            border_width=2,
            border_color=SENSOR_COLORS[index],
            **kwargs
        )

        self.index = index
        self._on_tare = on_tare
        self._on_toggle = on_toggle
        self.theme = theme

        self.name_label = ctk.CTkLabel(
            self,
            text=name.upper(),
            font=("Segoe UI", 16, "bold"),
            text_color=SENSOR_COLORS[index],
        )

        self.name_label.pack(
            pady=(18, 5),
            padx=12
        )

        self.enabled_var = ctk.BooleanVar(value=False)

        self.enabled_check = ctk.CTkCheckBox(
            self,
            text="Activo",
            variable=self.enabled_var,
            font=("Segoe UI", 13),
            text_color=theme["text"],
            command=lambda: self._on_toggle(
                self.index,
                bool(self.enabled_var.get())
            ),
        )

        self.enabled_check.pack(pady=(0, 8))

        self.value_var = ctk.StringVar(
            value="--.--"
        )

        self.value_label = ctk.CTkLabel(
            self,
            textvariable=self.value_var,
            font=("Segoe UI", 40, "bold"),
            text_color=theme["text"],
        )

        self.value_label.pack()

        self.unit_label = ctk.CTkLabel(
            self,
            text=unit,
            font=("Segoe UI", 15),
            text_color=theme["secondary_text"],
        )

        self.unit_label.pack(
            pady=(0, 8)
        )

        self.state_var = ctk.StringVar(
            value=STATE_INFO["none"][0]
        )

        self.state_label = ctk.CTkLabel(
            self,
            textvariable=self.state_var,
            font=("Segoe UI", 15, "bold"),
            text_color=STATE_INFO["none"][1],
        )

        self.state_label.pack(
            pady=(0, 12)
        )

        self.tare_button = ctk.CTkButton(
            self,
            text="Tara / 0",
            width=110,
            height=34,
            font=("Segoe UI", 13),
            corner_radius=8,
            fg_color=theme["secondary_button"],
            hover_color=theme["secondary_button_hover"],
            text_color=theme["text"],
            command=lambda: self._on_tare(self.index),
        )

        self.tare_button.pack(
            pady=(0, 18)
        )

    def update_value(
        self,
        value: float,
        state: str
    ) -> None:

        self.value_var.set(
            f"{value:.2f}"
        )

        text, color = STATE_INFO[state]

        self.state_var.set(text)
        self.state_label.configure(
            text_color=color
        )

    def set_unit(self, unit: str) -> None:
        self.unit_label.configure(
            text=unit
        )

    def set_name(self, name: str) -> None:
        self.name_label.configure(
            text=name.upper()
        )

    def set_enabled(self, enabled: bool) -> None:
        self.enabled_var.set(enabled)

    def is_enabled(self) -> bool:
        return bool(self.enabled_var.get())

    def apply_theme(
        self,
        theme: Dict[str, str]
    ) -> None:

        self.theme = theme

        self.configure(
            fg_color=theme["card"]
        )

        self.value_label.configure(
            text_color=theme["text"]
        )

        self.unit_label.configure(
            text_color=theme["secondary_text"]
        )

        self.enabled_check.configure(
            text_color=theme["text"]
        )

        self.tare_button.configure(
            fg_color=theme["secondary_button"],
            hover_color=theme["secondary_button_hover"],
            text_color=theme["text"]
        )


# =============================================================
# VENTANA DE CONFIGURACIÓN
# =============================================================

class ConfigWindow(ctk.CTkToplevel):

    def __init__(
        self,
        master,
        config: AppConfig,
        on_save: Callable[[AppConfig], None]
    ):
        super().__init__(master)

        self.title("⚙ Configuración")
        self.geometry("760x800")
        self.minsize(700, 700)
        self.resizable(True, True)

        self.config_data = config
        self._on_save = on_save

        self.theme = master.get_theme()

        self.configure(
            fg_color=self.theme["window"]
        )

        self.grab_set()

        self._build_ui()

    def _build_ui(self) -> None:

        theme = self.theme

        ctk.CTkLabel(
            self,
            text="Configuración general",
            font=("Segoe UI", 24, "bold"),
            text_color=theme["text"],
        ).pack(
            pady=(22, 14)
        )

        general = ctk.CTkFrame(
            self,
            fg_color=theme["panel"],
            corner_radius=12,
        )

        general.pack(
            padx=24,
            pady=6,
            fill="x"
        )

        self.read_freq = self._row(
            general,
            "Frecuencia de lectura (Hz):",
            str(self.config_data.read_frequency)
        )

        self.history_size = self._row(
            general,
            "Muestras en historial:",
            str(self.config_data.history_size)
        )

        self.baudrate = self._row(
            general,
            "Baudrate:",
            str(self.config_data.baudrate)
        )

        ctk.CTkLabel(
            self,
            text="Sensores",
            font=("Segoe UI", 22, "bold"),
            text_color=theme["text"],
        ).pack(
            pady=(18, 8)
        )

        sensors_frame = ctk.CTkScrollableFrame(
            self,
            fg_color=theme["panel"],
            corner_radius=12,
        )

        sensors_frame.pack(
            padx=24,
            pady=6,
            fill="both",
            expand=True
        )

        self.sensor_entries = []

        for sensor in self.config_data.sensors:

            row = ctk.CTkFrame(
                sensors_frame,
                fg_color="transparent"
            )

            row.pack(
                fill="x",
                pady=8,
                padx=10
            )

            ctk.CTkLabel(
                row,
                text=sensor.name,
                width=110,
                font=("Segoe UI", 14, "bold"),
                text_color=theme["text"],
            ).grid(
                row=0,
                column=0,
                padx=4
            )

            entries = {}

            for j, (label, key, value) in enumerate([
                ("Unidad", "unit", sensor.unit),
                ("Mín", "min", sensor.min_value),
                ("Máx", "max", sensor.max_value),
            ]):

                ctk.CTkLabel(
                    row,
                    text=label,
                    font=("Segoe UI", 12),
                    text_color=theme["secondary_text"],
                ).grid(
                    row=0,
                    column=1 + j * 2,
                    padx=(12, 3)
                )

                entry = ctk.CTkEntry(
                    row,
                    width=95,
                    justify="center",
                    font=("Segoe UI", 12),
                )

                entry.insert(
                    0,
                    str(value)
                )

                entry.grid(
                    row=0,
                    column=2 + j * 2,
                    padx=3
                )

                entries[key] = entry

            self.sensor_entries.append(entries)

        ctk.CTkButton(
            self,
            text="💾 Guardar cambios",
            height=46,
            font=("Segoe UI", 15, "bold"),
            corner_radius=10,
            fg_color=theme["button"],
            hover_color=theme["button_hover"],
            command=self._save,
        ).pack(
            pady=18,
            padx=24,
            fill="x"
        )

    def _row(
        self,
        parent,
        label: str,
        initial: str
    ) -> ctk.CTkEntry:

        frame = ctk.CTkFrame(
            parent,
            fg_color="transparent"
        )

        frame.pack(
            fill="x",
            pady=8,
            padx=16
        )

        ctk.CTkLabel(
            frame,
            text=label,
            font=("Segoe UI", 14),
            text_color=self.theme["text"],
            width=250,
            anchor="w"
        ).pack(
            side="left"
        )

        entry = ctk.CTkEntry(
            frame,
            width=140,
            justify="center",
            font=("Segoe UI", 13)
        )

        entry.insert(
            0,
            initial
        )

        entry.pack(
            side="right"
        )

        return entry

    def _save(self) -> None:

        try:
            self.config_data.read_frequency = int(
                self.read_freq.get()
            )

            self.config_data.history_size = int(
                self.history_size.get()
            )

            self.config_data.baudrate = int(
                self.baudrate.get()
            )

            for i, entries in enumerate(
                self.sensor_entries
            ):

                sensor = self.config_data.sensors[i]

                sensor.unit = (
                    entries["unit"].get().strip()
                    or "mm"
                )

                sensor.min_value = float(
                    entries["min"].get()
                )

                sensor.max_value = float(
                    entries["max"].get()
                )

            self._on_save(
                self.config_data
            )

            self.destroy()

        except ValueError as exc:
            messagebox.showerror(
                "Configuración",
                f"Valor inválido: {exc}"
            )


# =============================================================
# VENTANA DE CALIBRACIÓN
# =============================================================

class CalibrationWindow(ctk.CTkToplevel):

    def __init__(
        self,
        master,
        sensor_names: List[str],
        current_values: Dict[int, float],
        on_apply: Callable[[int, float], None]
    ):
        super().__init__(master)

        self.title("🎯 Calibración")
        self.geometry("520x400")
        self.resizable(False, False)

        self.theme = master.get_theme()

        self.configure(
            fg_color=self.theme["window"]
        )

        self.grab_set()

        self._current_values = current_values
        self._on_apply = on_apply

        self.sensor_names = sensor_names

        self._build_ui()

    def _build_ui(self) -> None:

        theme = self.theme

        ctk.CTkLabel(
            self,
            text="Calibración de sensor",
            font=("Segoe UI", 24, "bold"),
            text_color=theme["text"],
        ).pack(
            pady=(24, 12)
        )

        ctk.CTkLabel(
            self,
            text=(
                "Selecciona el sensor y ajusta su offset.\n"
                "El valor actual se convierte en 0 "
                "al pulsar Poner a 0."
            ),
            font=("Segoe UI", 13),
            text_color=theme["secondary_text"],
        ).pack(
            pady=(0, 18)
        )

        self.sensor_var = ctk.StringVar(
            value=self.sensor_names[0]
        )

        ctk.CTkOptionMenu(
            self,
            values=self.sensor_names,
            variable=self.sensor_var,
            width=300,
            height=42,
            font=("Segoe UI", 14),
            command=self._update_current,
        ).pack(
            pady=8
        )

        self.current_label = ctk.CTkLabel(
            self,
            text="Valor actual: --",
            font=("Segoe UI", 16, "bold"),
            text_color=theme["text"],
        )

        self.current_label.pack(
            pady=10
        )

        ctk.CTkLabel(
            self,
            text="Offset manual:",
            font=("Segoe UI", 14),
            text_color=theme["text"],
        ).pack()

        self.offset_entry = ctk.CTkEntry(
            self,
            width=170,
            justify="center",
            font=("Segoe UI", 14)
        )

        self.offset_entry.insert(
            0,
            "0.0"
        )

        self.offset_entry.pack(
            pady=8
        )

        btn_frame = ctk.CTkFrame(
            self,
            fg_color="transparent"
        )

        btn_frame.pack(
            pady=20
        )

        ctk.CTkButton(
            btn_frame,
            text="Aplicar offset",
            fg_color=theme["button"],
            hover_color=theme["button_hover"],
            width=150,
            height=42,
            corner_radius=8,
            font=("Segoe UI", 13, "bold"),
            command=self._apply_manual,
        ).pack(
            side="left",
            padx=7
        )

        ctk.CTkButton(
            btn_frame,
            text="Poner a 0",
            fg_color=theme["success"],
            hover_color=theme["success_hover"],
            width=150,
            height=42,
            corner_radius=8,
            font=("Segoe UI", 13, "bold"),
            command=self._apply_zero,
        ).pack(
            side="left",
            padx=7
        )

        self._update_current(
            self.sensor_names[0]
        )

    def _sensor_index(self) -> int:
        return int(
            self.sensor_var.get().split()[-1]
        ) - 1

    def _update_current(
        self,
        _=None
    ) -> None:

        idx = self._sensor_index()

        val = self._current_values.get(
            idx,
            0.0
        )

        self.current_label.configure(
            text=f"Valor actual: {val:.3f}"
        )

    def _apply_zero(self) -> None:

        idx = self._sensor_index()

        val = self._current_values.get(
            idx,
            0.0
        )

        self._on_apply(
            idx,
            -val
        )

        self.destroy()

    def _apply_manual(self) -> None:

        try:
            offset = float(
                self.offset_entry.get()
            )
        except ValueError:
            messagebox.showerror(
                "Calibración",
                "El offset debe ser un número."
            )
            return

        self._on_apply(
            self._sensor_index(),
            offset
        )

        self.destroy()


# =============================================================
# VENTANA PRINCIPAL
# =============================================================

class MainWindow(ctk.CTk):

    def __init__(self):

        super().__init__()

        self.title(
            "Monitor de Transductores Arduino"
        )

        # -----------------------------------------------------
        # ESCALA DE LA INTERFAZ
        # -----------------------------------------------------

        ctk.set_widget_scaling(UI_SCALE)
        ctk.set_window_scaling(1.0)

        # -----------------------------------------------------
        # PANTALLA COMPLETA
        # -----------------------------------------------------

        self.fullscreen = True

        try:
            self.attributes(
                "-fullscreen",
                True
            )
        except Exception:
            # Fallback para algunos sistemas.
            self.state("zoomed")

        self.bind(
            "<F11>",
            self._toggle_fullscreen
        )

        self.bind(
            "<Escape>",
            self._exit_fullscreen
        )

        self.minsize(
            1120,
            720
        )

        # -----------------------------------------------------
        # TEMA
        # -----------------------------------------------------

        self.theme = LIGHT_THEME

        self.configure(
            fg_color=self.theme["window"]
        )

        # -----------------------------------------------------
        # MODELO
        # -----------------------------------------------------

        self.config_data = AppConfig.load()

        self.serial = SerialManager(
            num_sensors=NUM_SENSORS
        )

        self.processor = DataProcessor(
            self.config_data
        )

        self.exporter = DataExporter(
            self.processor,
            self.config_data
        )

        self.serial.set_disconnect_callback(
            self._on_serial_disconnect
        )

        self.monitoring = False
        self.graph_paused = False

        self.current_values: Dict[int, float] = {
            i: 0.0
            for i in range(NUM_SENSORS)
        }

        self.start_time = time.time()

        # Referencias de widgets.
        self.header = None
        self.toolbar = None
        self.footer = None
        self.graph_wrapper = None
        self.graph_title_label = None
        self.cards: List[SensorCard] = []

        # -----------------------------------------------------
        # UI
        # -----------------------------------------------------

        self._build_toolbar()
        self._build_cards()
        self._build_graph()
        self._build_footer()

        self.protocol(
            "WM_DELETE_WINDOW",
            self._on_close
        )

        self._poll_serial()

        # Aplicar tema inicial.
        self._apply_theme()

    # =========================================================
    # FULLSCREEN
    # =========================================================

    def _toggle_fullscreen(
        self,
        _event=None
    ) -> None:

        self.fullscreen = not self.fullscreen

        try:
            self.attributes(
                "-fullscreen",
                self.fullscreen
            )

            if not self.fullscreen:
                self.geometry(
                    "1360x820"
                )

        except Exception:
            if self.fullscreen:
                self.state("zoomed")
            else:
                self.state("normal")

    def _exit_fullscreen(
        self,
        _event=None
    ) -> None:

        if self.fullscreen:
            self.fullscreen = False

            try:
                self.attributes(
                    "-fullscreen",
                    False
                )
                self.geometry(
                    "1360x820"
                )
            except Exception:
                self.state("normal")

    # =========================================================
    # THEME
    # =========================================================

    def get_theme(self) -> Dict[str, str]:
        return self.theme

    def _toggle_theme(self) -> None:

        current_mode = (
            ctk.get_appearance_mode()
            .lower()
        )

        if current_mode == "light":
            new_mode = "dark"
            self.theme = DARK_THEME
        else:
            new_mode = "light"
            self.theme = LIGHT_THEME

        ctk.set_appearance_mode(
            new_mode
        )

        self._apply_theme()

    def _apply_theme(self) -> None:

        theme = self.theme

        # Ventana principal.
        self.configure(
            fg_color=theme["window"]
        )

        # Header.
        if self.header:
            self.header.configure(
                fg_color=theme["header"]
            )

        # Toolbar.
        if self.toolbar:
            self.toolbar.configure(
                fg_color=theme["toolbar"]
            )

        # Footer.
        if self.footer:
            self.footer.configure(
                fg_color=theme["footer"]
            )

        # Graph wrapper.
        if self.graph_wrapper:
            self.graph_wrapper.configure(
                fg_color=theme["graph"]
            )

        # Actualizar tarjetas.
        for card in self.cards:
            card.apply_theme(theme)

        # Actualizar botones principales.
        if hasattr(self, "connect_btn"):
            if self.serial.is_connected:
                self.connect_btn.configure(
                    fg_color=theme["danger"],
                    hover_color=theme["danger_hover"]
                )
            else:
                self.connect_btn.configure(
                    fg_color=theme["button"],
                    hover_color=theme["button_hover"]
                )

        if hasattr(self, "start_btn"):
            self.start_btn.configure(
                fg_color=theme["success"],
                hover_color=theme["success_hover"]
            )

        if hasattr(self, "stop_btn"):
            self.stop_btn.configure(
                fg_color=theme["danger"],
                hover_color=theme["danger_hover"]
            )

        if hasattr(self, "pause_btn"):
            self.pause_btn.configure(
                fg_color=theme["secondary_button"],
                hover_color=theme[
                    "secondary_button_hover"
                ],
                text_color=theme["text"]
            )

        if hasattr(self, "clear_btn"):
            self.clear_btn.configure(
                fg_color=theme["secondary_button"],
                hover_color=theme[
                    "secondary_button_hover"
                ],
                text_color=theme["text"]
            )

        # Actualizar colores de labels.
        if hasattr(self, "graph_title_label"):
            self.graph_title_label.configure(
                text_color=theme["text"]
            )

        # Actualizar gráfica.
        self._apply_graph_theme()

    # =========================================================
    # UI: HEADER
    # =========================================================

    def _build_header(self) -> None:

        self.header = None

    # =========================================================
    # UI: TOOLBAR
    # =========================================================

    def _build_toolbar(self) -> None:

        theme = self.theme

        self.toolbar = ctk.CTkFrame(
            self,
            fg_color=theme["toolbar"],
            corner_radius=0,
            height=78
        )

        self.toolbar.pack(
            fill="x"
        )

        self.toolbar.pack_propagate(
            False
        )

        ctk.CTkLabel(
            self.toolbar,
            text="Puerto COM:",
            font=("Segoe UI", 15),
            text_color=theme["text"]
        ).pack(
            side="left",
            padx=(30, 8),
            pady=15
        )

        self.port_var = ctk.StringVar(
            value="--"
        )

        self.port_menu = ctk.CTkOptionMenu(
            self.toolbar,
            variable=self.port_var,
            values=["--"],
            width=165,
            height=42,
            font=("Segoe UI", 14)
        )

        self.port_menu.pack(
            side="left",
            padx=7
        )

        self.refresh_btn = ctk.CTkButton(
            self.toolbar,
            text="🔄",
            width=48,
            height=42,
            fg_color=theme["secondary_button"],
            hover_color=theme[
                "secondary_button_hover"
            ],
            text_color=theme["text"],
            font=("Segoe UI", 15),
            command=self._refresh_ports
        )

        self.refresh_btn.pack(
            side="left",
            padx=5
        )

        self.connect_btn = ctk.CTkButton(
            self.toolbar,
            text="🔌 Conectar",
            width=145,
            height=42,
            fg_color=theme["button"],
            hover_color=theme["button_hover"],
            font=("Segoe UI", 14, "bold"),
            corner_radius=8,
            command=self._toggle_connection
        )

        self.connect_btn.pack(
            side="right",
            padx=25
        )

        self.start_btn = ctk.CTkButton(
            self.toolbar,
            text="▶ Iniciar monitoreo",
            width=190,
            height=42,
            fg_color=theme["success"],
            hover_color=theme["success_hover"],
            font=("Segoe UI", 14, "bold"),
            corner_radius=8,
            command=self._start_monitoring,
            state="disabled"
        )

        self.start_btn.pack(
            side="left",
            padx=7
        )

        self.stop_btn = ctk.CTkButton(
            self.toolbar,
            text="■ Detener",
            width=130,
            height=42,
            fg_color=theme["danger"],
            hover_color=theme["danger_hover"],
            font=("Segoe UI", 14, "bold"),
            corner_radius=8,
            command=self._stop_monitoring,
            state="disabled"
        )

        self.stop_btn.pack(
            side="left",
            padx=7
        )

        self._refresh_ports()

    # =========================================================
    # UI: TARJETAS
    # =========================================================

    def _build_cards(self) -> None:

        container = ctk.CTkFrame(
            self,
            fg_color="transparent"
        )

        container.pack(
            fill="x",
            padx=22,
            pady=(18, 10)
        )

        for i in range(NUM_SENSORS):
            container.grid_columnconfigure(
                i,
                weight=1
            )

        self.cards = []

        for i, sensor in enumerate(
            self.config_data.sensors
        ):

            card = SensorCard(
                container,
                index=i,
                name=sensor.name,
                unit=sensor.unit,
                on_tare=self._on_tare,
                on_toggle=self._toggle_sensor,
                theme=self.theme
            )

            card.grid(
                row=0,
                column=i,
                padx=9,
                pady=5,
                sticky="nsew"
            )

            self.cards.append(card)

    # =========================================================
    # UI: GRÁFICA
    # =========================================================

    def _build_graph(self) -> None:

        theme = self.theme

        self.graph_wrapper = ctk.CTkFrame(
            self,
            fg_color=theme["graph"],
            corner_radius=14
        )

        self.graph_wrapper.pack(
            fill="both",
            expand=True,
            padx=22,
            pady=10
        )

        title_bar = ctk.CTkFrame(
            self.graph_wrapper,
            fg_color="transparent"
        )

        title_bar.pack(
            fill="x",
            padx=16,
            pady=(12, 0)
        )

        self.graph_title_label = ctk.CTkLabel(
            title_bar,
            text="📈 MONITOREO EN TIEMPO REAL",
            font=("Segoe UI", 17, "bold"),
            text_color=theme["text"]
        )

        self.graph_title_label.pack(
            side="left"
        )

        self.pause_btn = ctk.CTkButton(
            title_bar,
            text="⏸ Pausar",
            width=120,
            height=34,
            fg_color=theme["secondary_button"],
            hover_color=theme[
                "secondary_button_hover"
            ],
            text_color=theme["text"],
            font=("Segoe UI", 13),
            corner_radius=8,
            command=self._toggle_pause
        )

        self.pause_btn.pack(
            side="right",
            padx=4
        )

        self.clear_btn = ctk.CTkButton(
            title_bar,
            text="🧹 Limpiar",
            width=120,
            height=34,
            fg_color=theme["secondary_button"],
            hover_color=theme[
                "secondary_button_hover"
            ],
            text_color=theme["text"],
            font=("Segoe UI", 13),
            corner_radius=8,
            command=self._clear_graph
        )

        self.clear_btn.pack(
            side="right",
            padx=4
        )

        self.fig = Figure(
            figsize=(12, 4),
            dpi=100
        )

        self.ax = self.fig.add_subplot(111)

        self.lines = []

        for i in range(NUM_SENSORS):

            line, = self.ax.plot(
                [],
                [],
                color=SENSOR_COLORS[i],
                linewidth=2.2,
                label=self.config_data.sensors[i].name
            )

            self.lines.append(line)

        self._style_axes()

        self.canvas = FigureCanvasTkAgg(
            self.fig,
            master=self.graph_wrapper
        )

        self.canvas.draw()

        self.canvas.get_tk_widget().pack(
            fill="both",
            expand=True,
            padx=12,
            pady=(5, 12)
        )

    def _style_axes(self) -> None:

        theme = self.theme

        self.ax.set_facecolor(
            theme["graph"]
        )

        self.ax.tick_params(
            colors=theme["plot_text"],
            labelsize=11
        )

        for spine in self.ax.spines.values():
            spine.set_color(
                theme["plot_border"]
            )

        self.ax.grid(
            True,
            color=theme["plot_grid"],
            linestyle="--",
            linewidth=0.7,
            alpha=0.7
        )

        self.ax.set_xlabel(
            "Tiempo (s)",
            color=theme["plot_text"],
            fontsize=12
        )

        self.ax.set_ylabel(
            "Valor (mm)",
            color=theme["plot_text"],
            fontsize=12
        )

    def _apply_graph_theme(self) -> None:

        theme = self.theme

        self.fig.set_facecolor(
            theme["graph"]
        )

        self.ax.set_facecolor(
            theme["graph"]
        )

        self.ax.tick_params(
            colors=theme["plot_text"],
            labelsize=11
        )

        for spine in self.ax.spines.values():
            spine.set_color(
                theme["plot_border"]
            )

        self.ax.grid(
            True,
            color=theme["plot_grid"],
            linestyle="--",
            linewidth=0.7,
            alpha=0.7
        )

        self.ax.xaxis.label.set_color(
            theme["plot_text"]
        )

        self.ax.yaxis.label.set_color(
            theme["plot_text"]
        )

        legend = self.ax.get_legend()

        if legend:
            legend.get_frame().set_facecolor(
                theme["panel"]
            )

            legend.get_frame().set_edgecolor(
                theme["border"]
            )

            for text in legend.get_texts():
                text.set_color(
                    theme["text"]
                )

        self.canvas.draw_idle()

    # =========================================================
    # UI: FOOTER
    # =========================================================

    def _build_footer(self) -> None:

        theme = self.theme

        self.footer = ctk.CTkFrame(
            self,
            fg_color=theme["footer"],
            corner_radius=0,
            height=78
        )

        self.footer.pack(
            fill="x",
            side="bottom"
        )

        self.footer.pack_propagate(
            False
        )

        btn_cfg = dict(
            height=46,
            width=180,
            font=("Segoe UI", 14, "bold"),
            corner_radius=10
        )

        ctk.CTkButton(
            self.footer,
            text="🎯 Calibrar",
            fg_color=theme["purple"],
            hover_color=theme["purple_hover"],
            command=self._open_calibration,
            **btn_cfg
        ).pack(
            side="left",
            padx=14,
            pady=15
        )

        ctk.CTkButton(
            self.footer,
            text="⚙ Configuración",
            fg_color=theme["secondary_button"],
            hover_color=theme[
                "secondary_button_hover"
            ],
            text_color=theme["text"],
            command=self._open_config,
            **btn_cfg
        ).pack(
            side="left",
            padx=7,
            pady=15
        )

        ctk.CTkButton(
            self.footer,
            text="💾 Guardar CSV",
            fg_color=theme["button"],
            hover_color=theme["button_hover"],
            command=self._save_csv,
            **btn_cfg
        ).pack(
            side="left",
            padx=7,
            pady=15
        )

        ctk.CTkButton(
            self.footer,
            text="📄 Generar PDF",
            fg_color=theme["teal"],
            hover_color=theme["teal_hover"],
            command=self._save_pdf,
            **btn_cfg
        ).pack(
            side="left",
            padx=7,
            pady=15
        )

    # =========================================================
    # PUERTOS / CONEXIÓN
    # =========================================================

    def _refresh_ports(self) -> None:

        ports = SerialManager.list_ports()

        if not ports:
            ports = ["--"]

        self.port_menu.configure(
            values=ports
        )

        if self.port_var.get() not in ports:

            preferred = (
                self.config_data.port
                if self.config_data.port in ports
                else ports[0]
            )

            self.port_var.set(
                preferred
            )

    def _toggle_connection(self) -> None:

        if self.serial.is_connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:

        port = self.port_var.get()

        if not port or port == "--":
            messagebox.showwarning(
                "Puerto",
                "Selecciona un puerto COM válido."
            )
            return

        ok = self.serial.connect(
            port,
            self.config_data.baudrate
        )

        if ok:

            self.config_data.port = port
            self.config_data.save()

            self._set_connection_state(
                True
            )

            for index, card in enumerate(self.cards):
                if card.is_enabled():
                    self._set_sensor_enabled(index, True)

            self._start_monitoring()

        else:

            messagebox.showerror(
                "Conexión",
                f"No se pudo abrir {port}.\n"
                "Verifica el puerto y los permisos."
            )

    def _disconnect(self) -> None:

        self._stop_monitoring()

        for index, card in enumerate(self.cards):
            if card.is_enabled():
                self._set_sensor_enabled(index, False)

        self.serial.disconnect()

        self._set_connection_state(
            False
        )

    def _set_connection_state(
        self,
        connected: bool
    ) -> None:

        theme = self.theme

        if connected:
            self.connect_btn.configure(
                text="🔌 Desconectar",
                fg_color=theme["danger"],
                hover_color=theme["danger_hover"]
            )

            self.start_btn.configure(
                state="normal"
            )

        else:
            self.connect_btn.configure(
                text="🔌 Conectar",
                fg_color=theme["button"],
                hover_color=theme["button_hover"]
            )

            self.start_btn.configure(
                state="disabled"
            )

            self.stop_btn.configure(
                state="disabled"
            )

    def _on_serial_disconnect(self) -> None:

        self.after(
            0,
            self._handle_disconnect
        )

    def _handle_disconnect(self) -> None:

        if self.serial.is_connected:
            return

        self.monitoring = False

        self._set_connection_state(
            False
        )

        self.stop_btn.configure(
            state="disabled"
        )

    def _toggle_sensor(self, index: int, enabled: bool) -> None:
        if not self.serial.is_connected:
            self.cards[index].set_enabled(False)
            messagebox.showwarning(
                "Sensor",
                "Conecta el controlador antes de activar un sensor."
            )
            return

        self._set_sensor_enabled(index, enabled)

    def _set_sensor_enabled(self, index: int, enabled: bool) -> None:
        sensor_number = index + 1
        command = "E" if enabled else "D"
        light_command = "LON" if enabled else "LOFF"

        self.serial.send_command(f"{command}{sensor_number}")
        self.serial.send_command(f"{light_command}{sensor_number}")

    # =========================================================
    # MONITOREO
    # =========================================================

    def _start_monitoring(self) -> None:

        if not self.serial.is_connected:
            return

        self.processor.clear()

        self.start_time = time.time()

        self.monitoring = True

        self.start_btn.configure(
            state="disabled"
        )

        self.stop_btn.configure(
            state="normal"
        )

    def _stop_monitoring(self) -> None:

        self.monitoring = False

        if self.serial.is_connected:
            self.start_btn.configure(
                state="normal"
            )

        self.stop_btn.configure(
            state="disabled"
        )

    # =========================================================
    # BUCLE PRINCIPAL
    # =========================================================

    def _poll_serial(self) -> None:

        if self.monitoring:

            latest = None

            while True:

                data = self.serial.read_data_with_format()

                if data is None:
                    break

                latest = data

            if latest is not None:

                ts = (
                    time.time()
                    - self.start_time
                )

                raw_values, is_pot_format = latest

                results = self.processor.process(
                    raw_values,
                    ts,
                    invert_range=is_pot_format
                )

                self._apply_results(
                    results
                )

                if not self.graph_paused:
                    self._update_graph()

        self.after(
            REFRESH_MS,
            self._poll_serial
        )

    def _apply_results(
        self,
        results: List[Dict]
    ) -> None:

        for result in results:

            index = result["index"]

            self.current_values[index] = (
                result["value"]
            )

            self.cards[index].update_value(
                result["value"],
                result["state"]
            )

    def _update_graph(self) -> None:

        if not self.processor.timestamps:
            return

        xs = list(
            self.processor.timestamps
        )

        for i, line in enumerate(
            self.lines
        ):

            ys = list(
                self.processor.history[i]
            )

            line.set_data(
                xs[-len(ys):],
                ys
            )

        self.ax.relim()

        self.ax.autoscale_view(
            scalex=True,
            scaley=True
        )

        self.canvas.draw_idle()

    def _toggle_pause(self) -> None:

        self.graph_paused = (
            not self.graph_paused
        )

        self.pause_btn.configure(
            text=(
                "▶ Reanudar"
                if self.graph_paused
                else "⏸ Pausar"
            )
        )

    def _clear_graph(self) -> None:

        self.processor.clear()

        for line in self.lines:
            line.set_data([], [])

        self.ax.relim()

        self.ax.autoscale_view()

        self.canvas.draw_idle()

    # =========================================================
    # TARA / CALIBRACIÓN
    # =========================================================

    def _on_tare(
        self,
        index: int
    ) -> None:

        if (
            not self.serial.is_connected
            or not self.monitoring
        ):

            messagebox.showinfo(
                "Tara",
                "Conecta e inicia el monitoreo "
                "antes de tarar."
            )

            return

        current = self.current_values.get(
            index,
            0.0
        )

        self.processor.tare(
            index,
            current
            - self.config_data.sensors[index].offset
        )

        self.config_data.save()

    def _open_calibration(self) -> None:

        names = [
            sensor.name
            for sensor in self.config_data.sensors
        ]

        CalibrationWindow(
            self,
            names,
            self.current_values,
            on_apply=self._apply_offset
        )

    def _apply_offset(
        self,
        index: int,
        offset: float
    ) -> None:

        self.processor.set_offset(
            index,
            offset
        )

        self.config_data.save()

    # =========================================================
    # CONFIGURACIÓN
    # =========================================================

    def _open_config(self) -> None:

        ConfigWindow(
            self,
            self.config_data,
            on_save=self._on_config_saved
        )

    def _on_config_saved(
        self,
        config: AppConfig
    ) -> None:

        config.save()

        self.processor.config = config

        self.processor.update_history_limit(
            config.history_size
        )

        for i, sensor in enumerate(
            config.sensors
        ):

            self.cards[i].set_name(
                sensor.name
            )

            self.cards[i].set_unit(
                sensor.unit
            )

            self.lines[i].set_label(
                sensor.name
            )

        self._refresh_legend()

        self.canvas.draw_idle()

    def _refresh_legend(self) -> None:

        legend = self.ax.get_legend()

        if legend:
            legend.remove()

        self.ax.legend(
            loc="upper left",
            facecolor=self.theme["panel"],
            edgecolor=self.theme["border"],
            labelcolor=self.theme["text"],
            fontsize=11,
            ncol=NUM_SENSORS
        )

    # =========================================================
    # EXPORTACIÓN CSV
    # =========================================================

    def _save_csv(self) -> None:

        if not self.processor.timestamps:

            messagebox.showinfo(
                "CSV",
                "No hay datos para exportar."
            )

            return

        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[
                ("CSV", "*.csv")
            ],
            initialfile=(
                f"monitoreo_"
                f"{datetime.now():%Y%m%d_%H%M%S}.csv"
            )
        )

        if not path:
            return

        try:

            self.exporter.save_csv(
                path
            )

            messagebox.showinfo(
                "CSV",
                f"Datos guardados en:\n{path}"
            )

        except Exception as exc:

            messagebox.showerror(
                "CSV",
                f"Error al guardar:\n{exc}"
            )

    # =========================================================
    # EXPORTACIÓN PDF
    # =========================================================

    def _save_pdf(self) -> None:

        if not self.processor.timestamps:

            messagebox.showinfo(
                "PDF",
                "No hay datos para exportar."
            )

            return

        path = filedialog.asksaveasfilename(
            defaultextension=".pdf",
            filetypes=[
                ("PDF", "*.pdf")
            ],
            initialfile=(
                f"reporte_"
                f"{datetime.now():%Y%m%d_%H%M%S}.pdf"
            )
        )

        if not path:
            return

        tmp_png = None

        try:

            tmp_png = os.path.join(
                tempfile.gettempdir(),
                f"chart_{int(time.time())}.png"
            )

            self.fig.savefig(
                tmp_png,
                dpi=120,
                facecolor=self.theme["graph"],
                bbox_inches="tight"
            )

            self.exporter.save_pdf(
                path,
                tmp_png
            )

            messagebox.showinfo(
                "PDF",
                f"Reporte generado en:\n{path}"
            )

        except Exception as exc:

            messagebox.showerror(
                "PDF",
                f"Error al generar PDF:\n{exc}"
            )

        finally:

            if (
                tmp_png
                and os.path.exists(tmp_png)
            ):

                try:
                    os.remove(tmp_png)
                except Exception:
                    pass

    # =========================================================
    # CIERRE
    # =========================================================

    def _on_close(self) -> None:

        try:
            self.serial.disconnect()
        except Exception:
            pass

        self.destroy()


# =============================================================
# ENTRY POINT
# =============================================================

def main() -> None:

    # ---------------------------------------------------------
    # IMPORTANTE:
    # El tema se establece ANTES de crear MainWindow.
    # Por eso la aplicación comienza en modo claro.
    # ---------------------------------------------------------

    ctk.set_appearance_mode("light")

    ctk.set_default_color_theme(
        "blue"
    )

    # Escala inicial.
    ctk.set_widget_scaling(
        UI_SCALE
    )

    ctk.set_window_scaling(
        1.0
    )

    app = MainWindow()

    app.mainloop()


# =============================================================
# START
# =============================================================

if __name__ == "__main__":
    main()
