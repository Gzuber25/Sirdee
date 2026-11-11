"""Monitor serial de cinco transductores Arduino."""

import platform
import time
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FormatStrFormatter

from sirdee.constants import HISTORY_SIZE, NUM_SENSORS, PLOT_MS, POLL_MS, SENSOR_COLORS
from sirdee.reports import export_csv as write_csv
from sirdee.reports import export_pdf as write_pdf
from sirdee.sensors import parse_sensor_line, sensor_state
from sirdee.serial_reader import SerialReader

COLORS = SENSOR_COLORS


class ArduinoMonitor:
    def __init__(self, root):
        self.root = root
        self.root.title("Monitor de Transductores Arduino")
        self.root.geometry("1360x820")
        self.root.minsize(1120, 720)
        self.root.attributes("-fullscreen", True)
        self._detect_sbc_fullscreen()
        self.root.bind("<F11>", lambda _event: self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen")))
        self.root.bind("<Escape>", lambda _event: self.root.attributes("-fullscreen", False))
        if platform.system() == "Windows":
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass

        self.serial = SerialReader()
        self.recording = False
        self.paused = False
        self.start_time = time.time()
        self.terminal = None
        self.sensors = {
            f"Pot{i}": {
                "enabled": False,
                "values": deque(maxlen=HISTORY_SIZE),
                "times": deque(maxlen=HISTORY_SIZE),
                "all_values": [],
                "all_times": [],
                "range": 25.0,
                "offset": 0.0,
                "tared": False,
                "min": None,
                "max": None,
            }
            for i in range(1, NUM_SENSORS + 1)
        }
        self.vars = {}
        self.value_labels = {}
        self.state_labels = {}
        self.tare_buttons = {}
        self.range_entries = {}
        self.lines = {}

        self._build_custom_ui()
        self.refresh_ports()
        self._poll_serial()
        self._update_plot()
        self._update_status()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _detect_sbc_fullscreen(self):
        model_path = Path("/proc/device-tree/model")
        if not model_path.exists():
            return
        try:
            model = model_path.read_text(encoding="utf-8", errors="ignore").lower()
            if any(name in model for name in ("raspberry", "radxa", "rock")):
                self.root.attributes("-fullscreen", True)
        except (OSError, tk.TclError):
            pass

    def _build_custom_ui(self):
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        ctk.set_widget_scaling(1.3)

        self.root.configure(fg_color="#f2f4f7")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        self.toolbar = ctk.CTkFrame(self.root, fg_color="#ffffff", corner_radius=0, height=78)
        self.toolbar.grid(row=0, column=0, sticky="ew")
        self.toolbar.grid_propagate(False)

        ctk.CTkButton(
            self.toolbar,
            text="✕ Cerrar",
            width=88,
            height=34,
            fg_color="#c0392b",
            hover_color="#e74c3c",
            font=("Segoe UI", 14, "bold"),
            command=self.close,
        ).pack(side="left", padx=7)

        self.start_monitor_button = ctk.CTkButton(
            self.toolbar,
            text="▶ Iniciar monitoreo",
            width=152,
            height=34,
            fg_color="#27ae60",
            hover_color="#2ecc71",
            font=("Segoe UI", 14, "bold"),
            command=self.start_monitoring,
            state="disabled",
        )
        self.start_monitor_button.pack(side="left", padx=7)

        self.stop_monitor_button = ctk.CTkButton(
            self.toolbar,
            text="■ Detener",
            width=104,
            height=34,
            fg_color="#c0392b",
            hover_color="#e74c3c",
            font=("Segoe UI", 14, "bold"),
            command=self.stop_monitoring,
            state="disabled",
        )
        self.stop_monitor_button.pack(side="left", padx=7)

        self.connect_button = ctk.CTkButton(
            self.toolbar,
            text="🔌 Conectar",
            width=124,
            height=34,
            fg_color="#2f6fad",
            hover_color="#3f82c1",
            font=("Segoe UI", 14, "bold"),
            command=self.toggle_connection,
        )
        self.connect_button.pack(side="right", padx=24)

        ctk.CTkLabel(
            self.toolbar,
            text="Puerto COM:",
            font=("Segoe UI", 14),
            text_color="#1f2937",
        ).pack(side="right", padx=(8, 0))

        self.port_combo = ctk.CTkOptionMenu(
            self.toolbar,
            values=["--"],
            width=160,
            height=40,
            font=("Segoe UI", 13),
        )
        self.port_combo.pack(side="right", padx=7)
        ctk.CTkButton(
            self.toolbar,
            text="↻",
            width=36,
            height=32,
            fg_color="#dce3eb",
            hover_color="#cbd5df",
            text_color="#1f2937",
            font=("Segoe UI", 20),
            command=self.refresh_ports,
        ).pack(side="right", padx=4)

        content = ctk.CTkFrame(self.root, fg_color="transparent")
        content.grid(row=1, column=0, sticky="nsew", padx=22, pady=(16, 8))
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        cards = ctk.CTkFrame(content, fg_color="transparent")
        cards.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for index in range(NUM_SENSORS):
            cards.grid_columnconfigure(index, weight=1)
            self._build_custom_sensor_card(cards, index)

        graph = ctk.CTkFrame(content, fg_color="#ffffff", corner_radius=14)
        graph.grid(row=1, column=0, sticky="nsew")
        graph.columnconfigure(0, weight=1)
        graph.rowconfigure(1, weight=1)

        title_bar = ctk.CTkFrame(graph, fg_color="transparent")
        title_bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 0))
        ctk.CTkLabel(title_bar, text="📈 MONITOREO EN TIEMPO REAL", font=("Segoe UI", 17, "bold"), text_color="#1f2937").pack(side="left")
        self.pause_button = ctk.CTkButton(title_bar, text="⏸ Pausar", width=96, height=28, fg_color="#dce3eb", hover_color="#cbd5df", text_color="#1f2937", command=self.toggle_pause)
        self.pause_button.pack(side="right", padx=4)
        ctk.CTkButton(title_bar, text="🧹 Limpiar", width=96, height=28, fg_color="#dce3eb", hover_color="#cbd5df", text_color="#1f2937", command=self.clear_data).pack(side="right", padx=4)

        self.figure = Figure(figsize=(12, 4), dpi=100)
        self.axis = self.figure.add_subplot(111)
        self.axis.set_xlabel("Tiempo (s)")
        self.axis.set_ylabel("Valor (mm)")
        self.axis.grid(True, alpha=0.35, linestyle="--")
        self.axis.yaxis.set_major_formatter(FormatStrFormatter("%.4f"))
        for index in range(1, NUM_SENSORS + 1):
            line, = self.axis.plot([], [], color=COLORS[index - 1], linewidth=2.2, label=f"Sensor {index}")
            self.lines[f"Pot{index}"] = line
        self.axis.legend(loc="upper left", ncol=NUM_SENSORS, fontsize=9)
        self.canvas = FigureCanvasTkAgg(self.figure, master=graph)
        self.canvas.draw()
        self.canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 12))

        self.footer = ctk.CTkFrame(self.root, fg_color="#ffffff", corner_radius=0, height=78)
        self.footer.grid(row=2, column=0, sticky="ew")
        self.footer.grid_propagate(False)
        self.record_indicator = ctk.CTkLabel(self.footer, text="", font=("Segoe UI", 14, "bold"), text_color="#e74c3c")
        self.record_indicator.pack(side="left", padx=18)
        self.port_label = ctk.CTkLabel(self.footer, text="Sin conexión", font=("Segoe UI", 12), text_color="#5b6470")
        self.port_label.pack(side="left", padx=8)
        ctk.CTkButton(self.footer, text="🎯 Calibrar", width=128, height=36, fg_color="#8e44ad", hover_color="#9b59b6", command=self.calibration).pack(side="right", padx=7, pady=15)
        ctk.CTkButton(self.footer, text="📄 PDF", width=112, height=36, fg_color="#16a085", hover_color="#1abc9c", command=self.export_pdf).pack(side="right", padx=7, pady=15)
        ctk.CTkButton(self.footer, text="💾 CSV", width=112, height=36, fg_color="#2f6fad", hover_color="#3f82c1", command=self.export_csv).pack(side="right", padx=7, pady=15)
        self.record_button = ctk.CTkButton(self.footer, text="▶ Iniciar captura", width=144, height=36, fg_color="#27ae60", hover_color="#2ecc71", command=self.toggle_recording, state="disabled")
        self.record_button.pack(side="right", padx=7, pady=15)

        self.status_connection = ctk.CTkLabel(self.root, text="● Desconectado", height=28, fg_color="#2c3e50", text_color="#e74c3c", anchor="w")
        self.status_connection.grid(row=3, column=0, sticky="ew")

    def _build_custom_sensor_card(self, parent, index):
        number = index + 1
        name = f"Pot{number}"
        color = COLORS[index]
        card = ctk.CTkFrame(parent, fg_color="#ffffff", border_width=2, border_color=color, corner_radius=12)
        card.grid(row=0, column=index, sticky="nsew", padx=6)
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text=f"SENSOR {number}", font=("Segoe UI", 16, "bold"), text_color=color).pack(pady=(18, 5), padx=12)
        variable = tk.BooleanVar(value=False)
        self.vars[name] = variable
        ctk.CTkCheckBox(card, text="Activo", variable=variable, font=("Segoe UI", 13), command=lambda sensor=name: self.toggle_sensor(sensor)).pack(pady=(0, 8))
        label = ctk.CTkLabel(card, text="--.--", font=("Segoe UI", 40, "bold"), text_color="#1f2937")
        label.pack()
        self.value_labels[name] = label
        ctk.CTkLabel(card, text="mm", font=("Segoe UI", 15), text_color="#5b6470").pack(pady=(0, 8))
        state = ctk.CTkLabel(card, text="⚪ Sin datos", font=("Segoe UI", 15, "bold"), text_color="#95a5a6")
        state.pack(pady=(0, 12))
        self.state_labels[name] = state
        tare = ctk.CTkButton(card, text="Tara / 0", width=88, height=28, font=("Segoe UI", 13), corner_radius=8, fg_color="#dce3eb", hover_color="#cbd5df", text_color="#1f2937", command=lambda sensor=name: self.tare(sensor))
        tare.pack(pady=(0, 18))
        self.tare_buttons[name] = tare
        range_row = ctk.CTkFrame(card, fg_color="transparent")
        range_row.pack(fill="x", padx=8, pady=(0, 10))
        entry = ctk.CTkEntry(range_row, width=78, justify="center")
        entry.insert(0, "25.0")
        entry.pack(side="left", expand=True, padx=2)
        self.range_entries[name] = entry
        ctk.CTkButton(range_row, text="Set", width=40, height=24, command=lambda sensor=name: self.set_range(sensor)).pack(side="left", padx=2)

    def refresh_ports(self):
        ports = self.serial.ports()
        values = ports or ["--"]
        self.port_combo.configure(values=values)
        self.port_combo.set(values[0])

    def toggle_connection(self):
        if self.serial.connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        port = self.port_combo.get()
        if not port or port == "--":
            messagebox.showwarning("Puerto", "Selecciona un puerto serial.")
            return
        if not self.serial.connect(port):
            messagebox.showerror("Conexion", f"No se pudo abrir {port}.")
            return
        self.connect_button.configure(text="🔌 Desconectar", fg_color="#c0392b", hover_color="#e74c3c")
        self.start_monitor_button.configure(state="normal")
        self.record_button.configure(state="normal")
        self.pause_button.configure(state="normal")
        self.port_label.configure(text=port)
        self.status_connection.configure(text="● Conectado", text_color="#2ecc71")
        self.log(f"Conectado a {port}")
        for number in range(1, NUM_SENSORS + 1):
            name = f"Pot{number}"
            if self.sensors[name]["enabled"]:
                self._send_sensor_commands(number, True)

    def disconnect(self):
        for number in range(1, NUM_SENSORS + 1):
            if self.sensors[f"Pot{number}"]["enabled"]:
                self._send_sensor_commands(number, False)
        self.serial.disconnect()
        self.connect_button.configure(text="🔌 Conectar", fg_color="#2f6fad", hover_color="#3f82c1")
        self.start_monitor_button.configure(state="disabled")
        self.stop_monitor_button.configure(state="disabled")
        self.record_button.configure(state="disabled", text="▶ Iniciar captura")
        self.pause_button.configure(state="disabled", text="⏸ Pausar")
        self.port_label.configure(text="Sin conexión")
        self.status_connection.configure(text="● Desconectado", text_color="#e74c3c")
        self.recording = False
        self.paused = False
        self.log("Desconectado")

    def _send_sensor_commands(self, number, enabled):
        if enabled:
            self.serial.send(f"E{number}")
            self.serial.send(f"LON{number}")
        else:
            self.serial.send(f"D{number}")
            self.serial.send(f"LOFF{number}")

    def toggle_sensor(self, name):
        if not self.serial.connected:
            self.vars[name].set(False)
            messagebox.showwarning("Sensor", "Conecta primero el Arduino.")
            return
        number = int(name.replace("Pot", ""))
        enabled = bool(self.vars[name].get())
        self.sensors[name]["enabled"] = enabled
        self._send_sensor_commands(number, enabled)
        self.log(f">>> {'E' if enabled else 'D'}{number}")
        self.log(f">>> {'LON' if enabled else 'LOFF'}{number}")

    def _poll_serial(self):
        if self.serial.connected:
            while True:
                line = self.serial.get_line()
                if line is None:
                    break
                if line.startswith("Pot"):
                    self.process_data(line)
                else:
                    self.process_range_message(line)
                    self.log(line)
        self.root.after(POLL_MS, self._poll_serial)

    def process_data(self, line):
        values = parse_sensor_line(line, NUM_SENSORS)
        if values is None:
            return
        for index, raw in enumerate(values):
            if raw is None:
                continue
            name = f"Pot{index + 1}"
            sensor = self.sensors[name]
            value = sensor["range"] - raw - sensor["offset"]
            now = time.time() - self.start_time
            sensor["values"].append(value)
            sensor["times"].append(now)
            self.value_labels[name].configure(text=f"{value:.2f}")
            state_text, state_color = self._sensor_state(value, sensor["range"])
            self.state_labels[name].configure(text=state_text, text_color=state_color)
            if self.recording:
                sensor["all_values"].append(value)
                sensor["all_times"].append(now)
                sensor["min"] = value if sensor["min"] is None else min(sensor["min"], value)
                sensor["max"] = value if sensor["max"] is None else max(sensor["max"], value)

    @staticmethod
    def _sensor_state(value, maximum):
        state, color = sensor_state(value, maximum)
        prefix = {"Normal": "🟢", "Advertencia": "🟡", "Fuera de rango": "🔴"}[state]
        return f"{prefix} {state}", color

    def process_range_message(self, line):
        if "Rango=" not in line and "Rango T" not in line:
            return
        try:
            if line.startswith("T"):
                parts = line.split()
                number = int(parts[0].replace("T", "").replace(":", ""))
                value = float(parts[-1].replace("mm", "").replace("Rango=", ""))
            else:
                parts = line.split()
                number = int(parts[2].replace("T", ""))
                value = float(parts[4])
            name = f"Pot{number}"
            if name in self.sensors:
                self.sensors[name]["range"] = value
                self.range_entries[name].delete(0, tk.END)
                self.range_entries[name].insert(0, f"{value:.4f}")
        except (ValueError, IndexError):
            pass

    def _update_plot(self):
        for name, sensor in self.sensors.items():
            line = self.lines[name]
            if sensor["enabled"] and sensor["values"] and not self.paused:
                line.set_data(list(sensor["times"]), list(sensor["values"]))
                line.set_visible(True)
            elif not sensor["enabled"] or not sensor["values"]:
                line.set_visible(False)
        self.axis.relim()
        self.axis.autoscale_view()
        self.canvas.draw_idle()
        self.root.after(PLOT_MS, self._update_plot)

    def _update_status(self):
        samples = sum(len(sensor["all_values"]) for sensor in self.sensors.values())
        self.status_connection.configure(text=(f"● Conectado  |  Muestras: {samples:,}" if self.serial.connected and samples else ("● Conectado" if self.serial.connected else "● Desconectado")))
        minimum = None
        maximum = None
        for sensor in self.sensors.values():
            if sensor["enabled"] and sensor["min"] is not None:
                minimum = sensor["min"] if minimum is None else min(minimum, sensor["min"])
            if sensor["enabled"] and sensor["max"] is not None:
                maximum = sensor["max"] if maximum is None else max(maximum, sensor["max"])
        if minimum is not None and maximum is not None:
            self.port_label.configure(text=f"Min: {minimum:.2f}  Max: {maximum:.2f}")
        else:
            self.port_label.configure(text="Sin conexión" if not self.serial.connected else self.port_combo.get())
        if self.recording:
            self.record_indicator.configure(text="● REC", text_color="#e74c3c")
        else:
            self.record_indicator.configure(text="")
        self.root.after(500, self._update_status)

    def toggle_pause(self):
        self.paused = not self.paused
        self.pause_button.configure(text="▶ Reanudar" if self.paused else "⏸ Pausar")

    def start_monitoring(self):
        self.paused = False
        self.pause_button.configure(text="⏸ Pausar")
        self.start_monitor_button.configure(state="disabled")
        self.stop_monitor_button.configure(state="normal")

    def stop_monitoring(self):
        self.paused = True
        self.start_monitor_button.configure(state="normal")
        self.stop_monitor_button.configure(state="disabled")

    def clear_data(self):
        for sensor in self.sensors.values():
            sensor["values"].clear()
            sensor["times"].clear()
        for line in self.lines.values():
            line.set_data([], [])
        self.axis.relim()
        self.axis.autoscale_view()
        self.canvas.draw_idle()

    def toggle_recording(self):
        self.recording = not self.recording
        if self.recording:
            self.start_time = time.time()
            for sensor in self.sensors.values():
                sensor["values"].clear()
                sensor["times"].clear()
                sensor["all_values"].clear()
                sensor["all_times"].clear()
                sensor["min"] = None
                sensor["max"] = None
            self.record_button.configure(text="■ Detener captura", fg_color="#c0392b", hover_color="#e74c3c")
            self.log("Captura iniciada")
        else:
            self.record_button.configure(text="▶ Iniciar captura", fg_color="#27ae60", hover_color="#2ecc71")
            self.log("Captura detenida")

    def tare(self, name):
        sensor = self.sensors[name]
        if not sensor["tared"] and sensor["values"]:
            sensor["offset"] += sensor["values"][-1]
            sensor["tared"] = True
            self.tare_buttons[name].configure(text="Restaurar")
        else:
            sensor["offset"] = 0.0
            sensor["tared"] = False
            self.tare_buttons[name].configure(text="Poner a 0")

    def set_range(self, name):
        try:
            value = float(self.range_entries[name].get())
            if value <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Rango", "Escribe un rango positivo.")
            return
        number = int(name.replace("Pot", ""))
        self.sensors[name]["range"] = value
        if self.serial.send(f"R{number},{value}"):
            self.log(f">>> R{number},{value}")

    def calibration(self):
        if not self.serial.connected:
            messagebox.showwarning("Calibracion", "Conecta primero el Arduino.")
            return
        popup = tk.Toplevel(self.root)
        popup.title("Panel de calibracion")
        popup.geometry("520x360")
        popup.transient(self.root)
        text = tk.Text(popup, bg="#1a252f", fg="#ecf0f1")
        text.pack(fill="both", expand=True, padx=8, pady=8)

        buttons = ttk.Frame(popup)
        buttons.pack(fill="x", padx=8, pady=5)
        for number in range(1, NUM_SENSORS + 1):
            ttk.Button(buttons, text=str(number), command=lambda n=number: self._calibrate_sensor(n)).pack(side="left", padx=2)
        ttk.Button(buttons, text="Guardar (S)", command=lambda: self.serial.send("S")).pack(side="left", padx=8)
        ttk.Button(popup, text="Cerrar", command=popup.destroy).pack(pady=8)
        self.terminal = text

    def _calibrate_sensor(self, number):
        self.serial.send(f"LBLINK{number}")
        self.serial.send("C")
        self.root.after(500, lambda: self.serial.send(str(number)))
        self.root.after(600, lambda: self.serial.send(f"LON{number}"))

    def log(self, text):
        if self.terminal is not None and self.terminal.winfo_exists():
            self.terminal.insert(tk.END, text + "\n")
            self.terminal.see(tk.END)

    def export_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        enabled = [name for name, sensor in self.sensors.items() if sensor["enabled"]]
        if not enabled:
            messagebox.showwarning("CSV", "Activa al menos un sensor.")
            return
        try:
            write_csv(Path(path), self.sensors)
            messagebox.showinfo("CSV", "Datos exportados correctamente.")
        except OSError as exc:
            messagebox.showerror("CSV", str(exc))

    def export_pdf(self):
        path = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if not path:
            return
        enabled = [name for name, sensor in self.sensors.items() if sensor["all_values"]]
        if not enabled:
            messagebox.showwarning("PDF", "No hay datos grabados.")
            return
        try:
            write_pdf(Path(path), self.figure, self.sensors)
            messagebox.showinfo("PDF", "Reporte generado correctamente.")
        except Exception as exc:
            messagebox.showerror("PDF", str(exc))

    def close(self):
        self.serial.disconnect()
        self.root.destroy()


def main():
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    ArduinoMonitor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
