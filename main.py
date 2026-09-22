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
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_ui(self):
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TButton", padding=7, font=("Arial", 10, "bold"))
        style.configure("Small.TButton", padding=(5, 3), font=("Arial", 9, "bold"))

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        top = ttk.LabelFrame(self.root, text="Control de conexion", padding=8)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        top.columnconfigure(8, weight=1)

        ttk.Label(top, text="Puerto:").grid(row=0, column=0, padx=4)
        self.port_combo = ttk.Combobox(top, width=14, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=4)
        ttk.Button(top, text="Actualizar", command=self.refresh_ports).grid(row=0, column=2, padx=4)
        self.connect_button = ttk.Button(top, text="Conectar", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=3, padx=4)
        self.record_button = ttk.Button(top, text="Iniciar captura", command=self.toggle_recording, state="disabled")
        self.record_button.grid(row=0, column=4, padx=4)
        ttk.Button(top, text="Calibracion", command=self.calibration).grid(row=0, column=5, padx=4)
        ttk.Button(top, text="Guardar CSV", command=self.export_csv).grid(row=0, column=6, padx=4)
        ttk.Button(top, text="Guardar PDF", command=self.export_pdf).grid(row=0, column=7, padx=4)

        body = ttk.Frame(self.root)
        body.grid(row=1, column=0, sticky="nsew", padx=8)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        cards = ttk.Frame(body)
        cards.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for index in range(NUM_SENSORS):
            cards.columnconfigure(index, weight=1)
            self._build_sensor_card(cards, index)

        graph_box = ttk.LabelFrame(body, text="Monitoreo en tiempo real", padding=5)
        graph_box.grid(row=1, column=0, sticky="nsew")
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

        self.terminal = tk.Text(self.root, height=5, state="disabled", bg="#1a252f", fg="#ecf0f1")
        self.terminal.grid(row=2, column=0, sticky="ew", padx=8, pady=(5, 8))

    def _build_sensor_card(self, parent, index):
        number = index + 1
        name = f"Pot{number}"
        card = tk.Frame(parent, bg=COLORS[index], padx=6, pady=6)
        card.grid(row=0, column=index, sticky="nsew", padx=3)
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)

        variable = tk.BooleanVar(value=False)
        self.vars[name] = variable
        check = tk.Checkbutton(
            card,
            text=f"Sensor {number}",
            variable=variable,
            command=lambda sensor=name: self.toggle_sensor(sensor),
            bg=COLORS[index],
            fg="white",
            selectcolor=COLORS[index],
            activebackground=COLORS[index],
            activeforeground="white",
            font=("Arial", 11, "bold"),
        )
        check.grid(row=0, column=0, sticky="w")

        label = tk.Label(card, text="---.---- mm", bg=COLORS[index], fg="white", font=("Courier New", 16, "bold"))
        label.grid(row=0, column=1, sticky="e")
        self.value_labels[name] = label

        tare = ttk.Button(card, text="Poner a 0", style="Small.TButton", command=lambda sensor=name: self.tare(sensor))
        tare.grid(row=1, column=0, padx=2, pady=4, sticky="ew")
        self.tare_buttons[name] = tare

        entry = ttk.Entry(card, width=8, justify="center")
        entry.insert(0, "25.0")
        entry.grid(row=1, column=1, padx=2, pady=4, sticky="ew")
        self.range_entries[name] = entry
        ttk.Button(card, text="Set rango", style="Small.TButton", command=lambda sensor=name: self.set_range(sensor)).grid(row=2, column=0, columnspan=2, sticky="ew")

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
        self.recording = False
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
