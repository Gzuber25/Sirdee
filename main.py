"""Monitor serial de cinco transductores Arduino."""

import csv
import os
import platform
import queue
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

import serial
import serial.tools.list_ports
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FormatStrFormatter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


NUM_SENSORS = 5
BAUDRATE = 9600
POLL_MS = 50
PLOT_MS = 100
HISTORY_SIZE = 500
COLORS = ["#e74c3c", "#3498db", "#2ecc71", "#f39c12", "#9b59b6"]


class SerialReader:
    """Reads complete serial lines in a worker and never touches Tk widgets."""

    def __init__(self):
        self.connection = None
        self.thread = None
        self.stop_event = threading.Event()
        self.lines = queue.Queue(maxsize=1000)
        self.connected = False

    def ports(self):
        return [item.device for item in serial.tools.list_ports.comports()]

    def connect(self, port):
        try:
            self.connection = serial.Serial(port, BAUDRATE, timeout=1)
            time.sleep(2)
            self.stop_event.clear()
            self.connected = True
            self.thread = threading.Thread(target=self._read_loop, daemon=True)
            self.thread.start()
            return True
        except serial.SerialException as exc:
            print(f"Error al conectar: {exc}")
            self.connection = None
            self.connected = False
            return False

    def disconnect(self):
        self.stop_event.set()
        self.connected = False
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
        self.connection = None
        self.thread = None

    def send(self, command):
        if not self.connected or self.connection is None or not self.connection.is_open:
            return False
        try:
            payload = f"{command.rstrip(chr(10))}\n".encode("utf-8", errors="ignore")
            self.connection.write(payload)
            return True
        except (serial.SerialException, OSError) as exc:
            print(f"Error enviando comando: {exc}")
            return False

    def get_line(self):
        try:
            return self.lines.get_nowait()
        except queue.Empty:
            return None

    def _parse_line(self, line):
        if not line.strip().startswith("Pot"):
            return None
        values = [None] * NUM_SENSORS
        for part in line.replace("|", ",").split(","):
            if ":" not in part:
                continue
            name, value_text = [item.strip() for item in part.split(":", 1)]
            if not name.startswith("Pot"):
                continue
            try:
                index = int(name[3:]) - 1
                if 0 <= index < NUM_SENSORS:
                    values[index] = float(value_text)
            except ValueError:
                continue
        return (values, True) if any(value is not None for value in values) else None

    def _read_loop(self):
        while not self.stop_event.is_set():
            try:
                if self.connection is None or not self.connection.is_open:
                    break
                line = self.connection.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                if self.lines.full():
                    try:
                        self.lines.get_nowait()
                    except queue.Empty:
                        pass
                self.lines.put_nowait(line)
            except (serial.SerialException, OSError) as exc:
                print(f"Conexion perdida: {exc}")
                break
            except Exception as exc:
                print(f"Error leyendo serial: {exc}")
                time.sleep(0.05)
        self.connected = False


class ArduinoMonitor:
    def __init__(self, root):
        self.root = root
        self.root.title("Monitor de Transductores Arduino")
        self.root.geometry("1400x850")
        self.root.minsize(1000, 650)
        self._detect_sbc_fullscreen()
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
        self.tare_buttons = {}
        self.range_entries = {}
        self.lines = {}

        self._build_ui()
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

    def _build_ui(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self.font_normal = ("TkDefaultFont", 12)
        self.font_bold = ("TkDefaultFont", 12, "bold")
        self.font_large = ("TkDefaultFont", 20, "bold")
        self.font_small = ("TkDefaultFont", 10)
        style.configure("TButton", padding=(10, 8), font=self.font_bold)
        style.configure("Small.TButton", padding=(8, 5), font=self.font_small)
        style.configure("Connect.TButton", background="#2ecc71", foreground="white")
        style.configure("Disconnect.TButton", background="#e74c3c", foreground="white")
        style.configure("Action.TButton", background="#3498db", foreground="white")
        style.configure("Record.TButton", background="#2ecc71", foreground="white")
        style.configure("Report.TButton", background="#9b59b6", foreground="white")

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=0)

        main = tk.Frame(self.root, bg="#ecf0f1")
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(1, weight=1)
        main.rowconfigure(1, weight=1)

        connection = tk.Frame(main, bg="#34495e", height=58)
        connection.grid(row=0, column=0, columnspan=2, sticky="ew")
        connection.grid_propagate(False)
        connection.columnconfigure(4, weight=1)
        tk.Label(connection, text="Puerto:", font=self.font_normal, fg="white", bg="#34495e").grid(row=0, column=0, padx=(10, 4), pady=8)
        self.port_combo = ttk.Combobox(connection, width=14, state="readonly", font=self.font_normal)
        self.port_combo.grid(row=0, column=1, padx=4, pady=8)
        ttk.Button(connection, text="Actualizar", command=self.refresh_ports, style="Small.TButton").grid(row=0, column=2, padx=4, pady=8)
        self.connect_button = ttk.Button(connection, text="Conectar", command=self.toggle_connection, style="Connect.TButton")
        self.connect_button.grid(row=0, column=3, padx=4, pady=8)

        sensor_panel = tk.Frame(main, bg="#ecf0f1", width=440)
        sensor_panel.grid(row=1, column=0, sticky="ns")
        sensor_panel.grid_propagate(False)
        sensor_panel.columnconfigure(0, weight=1)
        for index in range(NUM_SENSORS):
            self._build_sensor_card(sensor_panel, index)

        actions = tk.Frame(sensor_panel, bg="#ecf0f1")
        actions.grid(row=NUM_SENSORS, column=0, sticky="ew", padx=6, pady=8)
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        ttk.Button(actions, text="Calibracion", command=self.calibration, style="Action.TButton").grid(row=0, column=0, sticky="ew", padx=3)
        ttk.Button(actions, text="Guardar CSV", command=self.export_csv, style="Report.TButton").grid(row=0, column=1, sticky="ew", padx=3)
        ttk.Button(actions, text="Guardar PDF", command=self.export_pdf, style="Report.TButton").grid(row=1, column=0, columnspan=2, sticky="ew", padx=3, pady=5)

        graph_box = tk.LabelFrame(main, text="Monitoreo de Transductores", bg="#ffffff", padx=5, pady=5)
        graph_box.grid(row=1, column=1, sticky="nsew", padx=(6, 8), pady=6)
        graph_box.rowconfigure(0, weight=1)
        graph_box.columnconfigure(0, weight=1)

        self.figure = Figure(figsize=(12, 5), dpi=90)
        self.axis = self.figure.add_subplot(111)
        self.axis.set_xlabel("Tiempo (s)")
        self.axis.set_ylabel("Valor (mm)")
        self.axis.grid(True, alpha=0.35, linestyle="--")
        self.axis.yaxis.set_major_formatter(FormatStrFormatter("%.4f"))
        for index in range(1, NUM_SENSORS + 1):
            line, = self.axis.plot([], [], color=COLORS[index - 1], linewidth=2, label=f"Sensor {index}")
            self.lines[f"Pot{index}"] = line
        self.axis.legend(loc="upper left", ncol=NUM_SENSORS, fontsize=9)
        self.canvas = FigureCanvasTkAgg(self.figure, master=graph_box)
        self.canvas.draw()
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

        bottom = tk.Frame(main, bg="#34495e", height=58)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew")
        bottom.grid_propagate(False)
        bottom.columnconfigure(1, weight=1)
        self.record_indicator = tk.Label(bottom, text="", font=self.font_bold, fg="#ecf0f1", bg="#34495e")
        self.record_indicator.grid(row=0, column=0, padx=10, pady=8)
        self.port_label = tk.Label(bottom, text="Sin conexion", font=self.font_small, fg="#ecf0f1", bg="#34495e")
        self.port_label.grid(row=0, column=1, sticky="w", padx=8)
        self.pause_button = ttk.Button(bottom, text="Pausar", command=self.toggle_pause, style="Action.TButton", state="disabled")
        self.pause_button.grid(row=0, column=2, padx=4, pady=6)
        self.record_button = ttk.Button(bottom, text="Iniciar captura", command=self.toggle_recording, style="Record.TButton", state="disabled")
        self.record_button.grid(row=0, column=3, padx=4, pady=6)

        self.status_frame = tk.Frame(self.root, bg="#2c3e50", height=32)
        self.status_frame.grid(row=1, column=0, sticky="ew")
        self.status_frame.grid_propagate(False)
        self.status_connection = tk.Label(self.status_frame, text="● Desconectado", font=self.font_small, fg="#e74c3c", bg="#2c3e50")
        self.status_connection.pack(side="left", padx=10)
        self.status_samples = tk.Label(self.status_frame, text="", font=self.font_small, fg="white", bg="#2c3e50")
        self.status_samples.pack(side="left", padx=10)
        self.status_minmax = tk.Label(self.status_frame, text="", font=self.font_small, fg="white", bg="#2c3e50")
        self.status_minmax.pack(side="right", padx=10)

    def _build_sensor_card(self, parent, index):
        number = index + 1
        name = f"Pot{number}"
        card = tk.Frame(parent, bg=COLORS[index], padx=8, pady=5)
        card.grid(row=index, column=0, sticky="ew", padx=5, pady=3)
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)

        variable = tk.BooleanVar(value=False)
        self.vars[name] = variable
        check = tk.Checkbutton(
            card,
            text=f"S{number}",
            variable=variable,
            command=lambda sensor=name: self.toggle_sensor(sensor),
            bg=COLORS[index],
            fg="white",
            selectcolor=COLORS[index],
            activebackground=COLORS[index],
            activeforeground="white",
            font=self.font_large,
        )
        check.grid(row=0, column=0, padx=(4, 8), sticky="w")

        label = tk.Label(card, text="---.---", bg=COLORS[index], fg="white", font=self.font_large, anchor="e")
        label.grid(row=0, column=1, padx=6, sticky="ew")
        self.value_labels[name] = label

        tare = ttk.Button(card, text="Poner a 0", style="Small.TButton", command=lambda sensor=name: self.tare(sensor))
        tare.grid(row=1, column=0, padx=2, pady=4, sticky="ew")
        self.tare_buttons[name] = tare

        entry = ttk.Entry(card, width=8, justify="center")
        entry.insert(0, "25.0")
        entry.grid(row=1, column=1, padx=2, pady=4, sticky="ew")
        self.range_entries[name] = entry
        ttk.Button(card, text="Set", style="Small.TButton", command=lambda sensor=name: self.set_range(sensor)).grid(row=2, column=0, columnspan=2, sticky="ew")

    def refresh_ports(self):
        ports = self.serial.ports()
        self.port_combo["values"] = ports or ["--"]
        if ports:
            self.port_combo.current(0)

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
        self.connect_button.config(text="Desconectar")
        self.record_button.config(state="normal")
        self.pause_button.config(state="normal")
        self.port_label.config(text=port)
        self.status_connection.config(text="● Conectado", fg="#2ecc71")
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
        self.connect_button.config(text="Conectar")
        self.record_button.config(state="disabled", text="Iniciar captura")
        self.pause_button.config(state="disabled", text="Pausar")
        self.port_label.config(text="Sin conexion")
        self.status_connection.config(text="● Desconectado", fg="#e74c3c")
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
        for part in line.replace("|", ",").split(","):
            if ":" not in part:
                continue
            name, value_text = [item.strip() for item in part.split(":", 1)]
            if name not in self.sensors:
                continue
            try:
                raw = float(value_text)
            except ValueError:
                continue
            sensor = self.sensors[name]
            value = sensor["range"] - raw - sensor["offset"]
            now = time.time() - self.start_time
            sensor["values"].append(value)
            sensor["times"].append(now)
            self.value_labels[name].config(text=f"{value:.4f} mm")
            if self.recording:
                sensor["all_values"].append(value)
                sensor["all_times"].append(now)
                sensor["min"] = value if sensor["min"] is None else min(sensor["min"], value)
                sensor["max"] = value if sensor["max"] is None else max(sensor["max"], value)

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
        self.status_samples.config(text=f"Muestras: {samples:,}" if samples else "")
        minimum = None
        maximum = None
        for sensor in self.sensors.values():
            if sensor["enabled"] and sensor["min"] is not None:
                minimum = sensor["min"] if minimum is None else min(minimum, sensor["min"])
            if sensor["enabled"] and sensor["max"] is not None:
                maximum = sensor["max"] if maximum is None else max(maximum, sensor["max"])
        if minimum is not None and maximum is not None:
            self.status_minmax.config(text=f"Min: {minimum:.2f}  Max: {maximum:.2f}")
        else:
            self.status_minmax.config(text="")
        if self.recording:
            self.record_indicator.config(text="● REC", fg="#e74c3c")
        else:
            self.record_indicator.config(text="")
        self.root.after(500, self._update_status)

    def toggle_pause(self):
        self.paused = not self.paused
        self.pause_button.config(text="Reanudar" if self.paused else "Pausar")

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
            self.record_button.config(text="Detener captura")
            self.log("Captura iniciada")
        else:
            self.record_button.config(text="Iniciar captura")
            self.log("Captura detenida")

    def tare(self, name):
        sensor = self.sensors[name]
        if not sensor["tared"] and sensor["values"]:
            sensor["offset"] += sensor["values"][-1]
            sensor["tared"] = True
            self.tare_buttons[name].config(text="Restaurar")
        else:
            sensor["offset"] = 0.0
            sensor["tared"] = False
            self.tare_buttons[name].config(text="Poner a 0")

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
            with open(Path(path), "w", newline="", encoding="utf-8") as output:
                writer = csv.writer(output)
                writer.writerow(["Tiempo (s)"] + [name.replace("Pot", "Sensor ") for name in enabled])
                length = max(len(self.sensors[name]["all_values"]) for name in enabled)
                for row_index in range(length):
                    first = self.sensors[enabled[0]]
                    if row_index >= len(first["all_times"]):
                        continue
                    row = [f"{first['all_times'][row_index]:.4f}"]
                    for name in enabled:
                        values = self.sensors[name]["all_values"]
                        row.append(f"{values[row_index]:.4f}" if row_index < len(values) else "")
                    writer.writerow(row)
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
        image_path = None
        try:
            image_path = os.path.join(tempfile.gettempdir(), "arduino_monitor_chart.png")
            self.figure.savefig(image_path, dpi=120, bbox_inches="tight")
            rows = [["Sensor", "Actual", "Min", "Max", "Muestras"]]
            for name in enabled:
                sensor = self.sensors[name]
                rows.append([name.replace("Pot", "Sensor "), f"{sensor['all_values'][-1]:.4f}", f"{min(sensor['all_values']):.4f}", f"{max(sensor['all_values']):.4f}", str(len(sensor['all_values']))])
            doc = SimpleDocTemplate(path, pagesize=A4)
            table = Table(rows)
            table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#34495e")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
            story = [Paragraph("Reporte de Transductores", getSampleStyleSheet()["Title"]), Spacer(1, 12), table, Spacer(1, 12), Image(image_path, width=16 * cm, height=8 * cm)]
            doc.build(story)
            messagebox.showinfo("PDF", "Reporte generado correctamente.")
        except Exception as exc:
            messagebox.showerror("PDF", str(exc))
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except OSError:
                    pass

    def close(self):
        self.serial.disconnect()
        self.root.destroy()


def main():
    root = tk.Tk()
    ArduinoMonitor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
