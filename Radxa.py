import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import serial
import queue
import serial.tools.list_ports
import matplotlib
matplotlib.use('TkAgg')
matplotlib.rcParams['toolbar'] = 'None'
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FormatStrFormatter
import threading
import time
from datetime import datetime
from collections import deque
import csv
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet
import os
import platform
from pathlib import Path


class ArduinoMonitor:
    def __init__(self, root):
        self.root = root
        self.root.title("Monitor de Transductores")
        self.root.geometry("800x480")
        self.root.minsize(800, 480)

        # Detect RPi/Radxa → fullscreen
        self._detect_sbc_fullscreen()

        # Escape to exit fullscreen (development)
        self.root.bind('<Escape>', lambda e: self.root.attributes('-fullscreen', False))

        self.serial_conn = None
        self.is_reading = False
        self.is_recording_session = False
        self.is_calibrating = False
        self.is_paused = False
        self.recording_start_time = None
        self.session_start_time = None

        # Thread-safe serial queue
        self.serial_queue = queue.Queue()

        # Reconnection state
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 10
        self.reconnect_delay = 2000  # ms
        self.is_reconnecting = False
        self.last_port = None

        self.pot_data = {
            'Pot1': {'values': deque(), 'times': deque(), 'all_values': [], 'all_times': [], 'enabled': False, 'color': '#e74c3c', 'offset': 0, 'min_session': None, 'max_session': None, 'is_tared': False},
            'Pot2': {'values': deque(), 'times': deque(), 'all_values': [], 'all_times': [], 'enabled': False, 'color': '#3498db', 'offset': 0, 'min_session': None, 'max_session': None, 'is_tared': False},
            'Pot3': {'values': deque(), 'times': deque(), 'all_values': [], 'all_times': [], 'enabled': False, 'color': '#2ecc71', 'offset': 0, 'min_session': None, 'max_session': None, 'is_tared': False},
            'Pot4': {'values': deque(), 'times': deque(), 'all_values': [], 'all_times': [], 'enabled': False, 'color': '#f39c12', 'offset': 0, 'min_session': None, 'max_session': None, 'is_tared': False},
            'Pot5': {'values': deque(), 'times': deque(), 'all_values': [], 'all_times': [], 'enabled': False, 'color': '#9b59b6', 'offset': 0, 'min_session': None, 'max_session': None, 'is_tared': False},
        }

        self.start_time = time.time()
        self.terminal_text = None

        self.setup_styles()
        self.setup_ui()
        self.update_ports()

        # Start serial queue processor (main thread, every 50ms)
        self.process_serial_queue()

        # Close confirmation
        self.root.protocol('WM_DELETE_WINDOW', self.on_closing)

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------

    def _detect_sbc_fullscreen(self):
        try:
            model_path = Path('/proc/device-tree/model')
            if model_path.exists():
                model = model_path.read_text().lower()
                if 'raspberry' in model or 'radxa' in model or 'rock' in model:
                    self.root.attributes('-fullscreen', True)
        except Exception:
            pass

    def setup_styles(self):
        style = ttk.Style()
        available_themes = style.theme_names()
        if 'clam' in available_themes:
            style.theme_use('clam')
        elif 'alt' in available_themes:
            style.theme_use('alt')

        # Touch-friendly fonts
        self.font_normal = ('TkDefaultFont', 16)
        self.font_bold = ('TkDefaultFont', 16, 'bold')
        self.font_large = ('TkDefaultFont', 24, 'bold')
        self.font_small = ('TkDefaultFont', 14)
        self.font_mono = ('DejaVu Sans Mono', 12) if platform.system() == 'Linux' else ('Courier New', 12)

        style.configure('TButton', font=self.font_bold, padding=(18, 16), borderwidth=0)
        style.map('TButton', relief=[('pressed', 'sunken'), ('!pressed', 'raised')])

        style.configure('TNotebook.Tab', font=self.font_large, padding=(20, 8))

        style.configure('Connect.TButton', background='#2ecc71', foreground='white')
        style.map('Connect.TButton', background=[('active', '#27ae60')])

        style.configure('Disconnect.TButton', background='#e74c3c', foreground='white')
        style.map('Disconnect.TButton', background=[('active', '#c0392b')])

        style.configure('Action.TButton', background='#3498db', foreground='white')
        style.map('Action.TButton', background=[('active', '#2980b9')])

        style.configure('Report.TButton', background='#9b59b6', foreground='white')
        style.map('Report.TButton', background=[('active', '#8e44ad')])

        style.configure('Record.TButton', background='#2ecc71', foreground='white')
        style.map('Record.TButton', background=[('active', '#27ae60')])

        style.configure('Small.TButton', font=self.font_small, padding=(14, 10),
                         foreground='black', background='#ecf0f1')
        style.map('Small.TButton', background=[('active', '#bdc3c7')])

        style.configure("Treeview", font=self.font_normal, rowheight=32,
                         background="#ffffff", fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=self.font_bold,
                         background="#dfe6e9", foreground="#2d3436")
        style.map("Treeview", background=[('selected', '#3498db')],
                  foreground=[('selected', 'white')])

        pot_colors = {
            'Pot1': '#e74c3c', 'Pot2': '#3498db', 'Pot3': '#2ecc71',
            'Pot4': '#f39c12', 'Pot5': '#9b59b6',
        }
        for pot_name, color in pot_colors.items():
            style.configure(f'{pot_name}.TCheckbutton', background=color,
                            foreground='white', font=self.font_bold)
            style.map(f'{pot_name}.TCheckbutton', background=[('active', color)])
            style.configure(f'{pot_name}.TLabel', background=color,
                            foreground='white', font=self.font_normal)

    # ------------------------------------------------------------------
    # UI layout
    # ------------------------------------------------------------------

    def setup_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=0)

        main_frame = ttk.Frame(self.root)
        main_frame.grid(row=0, column=0, sticky='nsew')

        self.build_tab_monitor(main_frame)
        self.build_status_bar(self.root)

    # ---- Tab 1: Monitor -------------------------------------------------

    def build_tab_monitor(self, parent):
        parent.columnconfigure(0, weight=0)   # left: sensor panel (fixed)
        parent.columnconfigure(1, weight=1)   # right: chart (expanding)
        parent.rowconfigure(0, weight=0)      # connection bar
        parent.rowconfigure(1, weight=1)      # main content
        parent.rowconfigure(2, weight=0)      # capture bar

        # -- Top: connection bar (full width) --
        conn_frame = tk.Frame(parent, bg='#34495e', height=50)
        conn_frame.grid(row=0, column=0, columnspan=2, sticky='ew')
        conn_frame.grid_propagate(False)
        conn_frame.columnconfigure(1, weight=0)
        conn_frame.columnconfigure(4, weight=1)

        tk.Label(conn_frame, text="Puerto:", font=self.font_normal,
                 fg='white', bg='#34495e').grid(row=0, column=0, padx=(8, 4), pady=6)

        self.port_combo = ttk.Combobox(conn_frame, width=14, state='readonly',
                                        font=self.font_normal)
        self.port_combo.grid(row=0, column=1, padx=4, pady=6)

        ttk.Button(conn_frame, text="Actualizar", command=self.update_ports,
                   style='Small.TButton').grid(row=0, column=2, padx=4, pady=6)

        self.connect_btn = ttk.Button(conn_frame, text="Conectar",
                                      command=self.toggle_connection,
                                      style='Connect.TButton')
        self.connect_btn.grid(row=0, column=3, padx=4, pady=6)

        # -- Left: sensor panel (fixed width) --
        sensor_panel = tk.Frame(parent, bg='#ecf0f1', width=440)
        sensor_panel.grid(row=1, column=0, sticky='ns')
        sensor_panel.grid_propagate(False)
        sensor_panel.columnconfigure(0, weight=1)

        self.pot_vars = {}
        self.pot_labels = {}
        self.monitor_labels = {}
        self.tare_buttons = {}
        self.range_entries = {}

        for i, pot_name in enumerate(['Pot1', 'Pot2', 'Pot3', 'Pot4', 'Pot5']):
            pot_info = self.pot_data[pot_name]
            pot_index = i + 1
            color = pot_info['color']

            # Card per sensor
            card = tk.Frame(sensor_panel, bg=color, bd=0, highlightthickness=0)
            card.grid(row=i, column=0, sticky='ew', padx=5, pady=4)
            card.columnconfigure(1, weight=1)

            var = tk.BooleanVar(value=False)
            self.pot_vars[pot_name] = var

            # Row 0: Checkbox + Value (BIG fonts)
            cb = tk.Checkbutton(card, text=f"S{pot_index}", variable=var,
                                command=lambda p=pot_name: self.toggle_pot(p),
                                bg=color, fg='white', selectcolor=color,
                                activebackground=color,
                                font=self.font_large, highlightthickness=0, bd=0)
            cb.grid(row=0, column=0, padx=(10, 4), pady=(8, 0), sticky='w')

            val_lbl = tk.Label(card, text="---.---", font=self.font_large,
                               fg='white', bg=color, anchor='e')
            val_lbl.grid(row=0, column=1, padx=(4, 10), pady=(8, 0), sticky='ew')
            self.pot_labels[pot_name] = val_lbl
            self.monitor_labels[pot_name] = val_lbl

            # Row 1: Tare + Range + Set (compact)
            ctrl_row = tk.Frame(card, bg=color)
            ctrl_row.grid(row=1, column=0, columnspan=2, sticky='ew', padx=8, pady=(4, 8))
            ctrl_row.columnconfigure(3, weight=1)

            tare_btn = ttk.Button(ctrl_row, text="Poner a 0", style='Small.TButton',
                                  command=lambda p=pot_name: self.set_zero(p))
            tare_btn.grid(row=0, column=0, padx=(0, 8), pady=4)
            self.tare_buttons[pot_name] = tare_btn

            tk.Label(ctrl_row, text="Rng:", font=self.font_small,
                     fg='white', bg=color).grid(row=0, column=1, padx=(6, 2), pady=4)

            range_entry = ttk.Entry(ctrl_row, width=7, font=self.font_small)
            range_entry.insert(0, "25.0")
            range_entry.grid(row=0, column=2, padx=3, pady=4)
            self.range_entries[pot_name] = range_entry

            ttk.Button(ctrl_row, text="Set", style='Small.TButton',
                       command=lambda p=pot_name, idx=pot_index: self.set_transducer_range(p, idx)
                       ).grid(row=0, column=3, padx=(3, 0), pady=4, sticky='w')

        # -- Action buttons below sensors --
        btn_panel = tk.Frame(sensor_panel, bg='#ecf0f1')
        btn_panel.grid(row=5, column=0, sticky='ew', padx=5, pady=(8, 4))
        btn_panel.columnconfigure(0, weight=1)
        btn_panel.columnconfigure(1, weight=1)

        ttk.Button(btn_panel, text="Calibración",
                   command=self.show_calibration_popup,
                   style='Action.TButton').grid(row=0, column=0, sticky='ew', padx=4, pady=4)

        self.csv_btn = ttk.Button(btn_panel, text="CSV",
                                  command=self.export_csv,
                                  style='Report.TButton', state='disabled')
        self.csv_btn.grid(row=0, column=1, sticky='ew', padx=4, pady=4)

        btn_panel2 = tk.Frame(sensor_panel, bg='#ecf0f1')
        btn_panel2.grid(row=6, column=0, sticky='ew', padx=5, pady=(0, 4))
        btn_panel2.columnconfigure(0, weight=1)

        self.pdf_btn = ttk.Button(btn_panel2, text="Guardar PDF",
                                  command=self.generate_pdf_report,
                                  style='Report.TButton', state='disabled')
        self.pdf_btn.grid(row=0, column=0, sticky='ew', padx=4, pady=4)

        # -- Right: chart --
        chart_frame = tk.Frame(parent)
        chart_frame.grid(row=1, column=1, sticky='nsew')
        chart_frame.columnconfigure(0, weight=1)
        chart_frame.rowconfigure(0, weight=1)

        self.fig = Figure(figsize=(5, 3), dpi=100)
        self.ax_main = self.fig.add_subplot(111)
        self.ax_main.set_xlabel('Tiempo (s)', fontsize=11)
        self.ax_main.set_ylabel('mm', fontsize=11)
        self.ax_main.grid(True, alpha=0.2, linestyle='--')
        self.ax_main.tick_params(labelsize=10)
        self.ax_main.yaxis.set_major_formatter(FormatStrFormatter('%.2f'))
        self.fig.tight_layout(pad=1.5)

        self.lines = {}
        for pot_name, pot_info in self.pot_data.items():
            line, = self.ax_main.plot([], [], color=pot_info['color'],
                                      linewidth=2.0,
                                      label=pot_name.replace('Pot', 'S'))
            self.lines[pot_name] = line

        self.ax_main.legend(loc='upper left', fontsize=9, ncol=5, framealpha=0.7)

        self.canvas_main = FigureCanvasTkAgg(self.fig, master=chart_frame)
        self.canvas_main.draw()
        self.canvas_main.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self.min_max_text = self.ax_main.text(
            0.98, 0.97, '', transform=self.ax_main.transAxes, fontsize=10,
            verticalalignment='top', horizontalalignment='right',
            bbox=dict(boxstyle='round,pad=0.3', fc='wheat', alpha=0.7))

        # -- Bottom: capture bar (full width) --
        bottom_frame = tk.Frame(parent, bg='#34495e', height=50)
        bottom_frame.grid(row=2, column=0, columnspan=2, sticky='ew')
        bottom_frame.grid_propagate(False)
        bottom_frame.columnconfigure(1, weight=1)

        self.rec_indicator = tk.Label(bottom_frame, text="", font=self.font_bold,
                                      fg='#ecf0f1', bg='#34495e')
        self.rec_indicator.grid(row=0, column=0, padx=8, pady=6)

        self.port_label = tk.Label(bottom_frame, text="Sin conexión",
                                   font=self.font_small, fg='#95a5a6', bg='#34495e')
        self.port_label.grid(row=0, column=1, padx=8, pady=6, sticky='w')

        self.pause_btn = ttk.Button(bottom_frame, text="Pausar",
                                    command=self.toggle_pause,
                                    state='disabled', style='Action.TButton')
        self.pause_btn.grid(row=0, column=2, padx=4, pady=4)

        self.record_btn = ttk.Button(bottom_frame, text="Iniciar captura",
                                     command=self.toggle_recording,
                                     state='disabled', style='Record.TButton')
        self.record_btn.grid(row=0, column=3, padx=4, pady=4)

    # (Calibración and Exportar buttons are now in the sensor panel)

    # ---- Global status bar (outside Notebook) ----------------------------

    def build_status_bar(self, parent):
        self.status_frame = tk.Frame(parent, bg='#2c3e50', height=30)
        self.status_frame.grid(row=1, column=0, sticky='ew')
        self.status_frame.grid_propagate(False)
        self.status_frame.columnconfigure(3, weight=1)

        self.status_conn = tk.Label(self.status_frame, text="● Desconectado",
                                    font=self.font_small, fg='#e74c3c', bg='#2c3e50')
        self.status_conn.grid(row=0, column=0, padx=8, pady=4)

        self.status_rec = tk.Label(self.status_frame, text="",
                                   font=self.font_small, fg='#e74c3c', bg='#2c3e50')
        self.status_rec.grid(row=0, column=1, padx=8, pady=4)

        self.status_samples = tk.Label(self.status_frame, text="",
                                       font=self.font_small, fg='#ecf0f1', bg='#2c3e50')
        self.status_samples.grid(row=0, column=2, padx=8, pady=4)

        self.status_minmax = tk.Label(self.status_frame, text="",
                                      font=self.font_small, fg='#ecf0f1', bg='#2c3e50')
        self.status_minmax.grid(row=0, column=3, padx=8, pady=4, sticky='e')

        self.update_status_bar()

    # ------------------------------------------------------------------
    # Status bar updates (every 500ms)
    # ------------------------------------------------------------------

    def update_status_bar(self):
        # Connection indicator
        if self.is_reconnecting:
            self.status_conn.config(text="● Reconectando...", fg='#f39c12')
        elif self.is_reading:
            self.status_conn.config(text="● Conectado", fg='#2ecc71')
        else:
            self.status_conn.config(text="● Desconectado", fg='#e74c3c')

        # REC indicator
        if self.is_recording_session and self.recording_start_time:
            elapsed = time.time() - self.recording_start_time
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            s = int(elapsed % 60)
            rec_text = f"● REC {h:02d}:{m:02d}:{s:02d}"
            self.status_rec.config(text=rec_text, fg='#e74c3c')
            self.rec_indicator.config(text=rec_text, fg='#e74c3c')
        else:
            self.status_rec.config(text="")
            self.rec_indicator.config(text="")

        # Sample count
        total_samples = sum(len(info['all_values']) for info in self.pot_data.values())
        if total_samples > 0:
            self.status_samples.config(text=f"Muestras: {total_samples:,}")
        else:
            self.status_samples.config(text="")

        # Global min/max
        min_session = None
        max_session = None
        for pot_info in self.pot_data.values():
            if pot_info['enabled'] and pot_info['min_session'] is not None:
                if min_session is None or pot_info['min_session'] < min_session:
                    min_session = pot_info['min_session']
            if pot_info['enabled'] and pot_info['max_session'] is not None:
                if max_session is None or pot_info['max_session'] > max_session:
                    max_session = pot_info['max_session']

        if min_session is not None and max_session is not None:
            self.status_minmax.config(text=f"Min: {min_session:.2f}  Max: {max_session:.2f}")
        else:
            self.status_minmax.config(text="")

        self.root.after(500, self.update_status_bar)

    # ------------------------------------------------------------------
    # Thread-safe serial communication
    # ------------------------------------------------------------------

    def process_serial_queue(self):
        """Drain the serial queue and update widgets (main thread, every 50ms)."""
        try:
            while True:
                msg = self.serial_queue.get_nowait()

                if msg[0] == 'data':
                    self._process_data_line(msg[1])
                elif msg[0] == 'terminal':
                    line = msg[1]
                    if self.terminal_text:
                        try:
                            self.terminal_text.insert(tk.END, line + '\n')
                            self.terminal_text.see(tk.END)
                        except tk.TclError:
                            pass
                elif msg[0] == 'range':
                    pot_name, value = msg[1], msg[2]
                    if pot_name in self.range_entries:
                        self.range_entries[pot_name].delete(0, tk.END)
                        self.range_entries[pot_name].insert(0, value)
                elif msg[0] == 'connection_lost':
                    self.attempt_reconnect()
        except queue.Empty:
            pass

        self.root.after(50, self.process_serial_queue)

    def read_serial(self):
        """Read serial data in background thread. Only puts messages in queue."""
        while self.is_reading:
            try:
                if self.serial_conn and self.serial_conn.in_waiting:
                    line = self.serial_conn.readline().decode('utf-8', errors='ignore').strip()
                    if not line:
                        continue

                    if line.startswith("Pot"):
                        self.serial_queue.put(('data', line))
                    else:
                        # Parse range info
                        if "Rango=" in line or "Rango T" in line:
                            try:
                                parts = line.split()
                                t_index = -1
                                r_value = ""
                                if line.startswith("T"):
                                    t_index = int(parts[0].replace('T', '').replace(':', ''))
                                    r_value = parts[-1].replace('mm', '').replace('Rango=', '')
                                elif "Rango T" in line:
                                    t_index = int(parts[2].replace('T', ''))
                                    r_value = parts[4]

                                if t_index != -1 and r_value:
                                    pot_name = f"Pot{t_index}"
                                    try:
                                        formatted = f"{float(r_value):.4f}"
                                    except ValueError:
                                        formatted = r_value
                                    self.serial_queue.put(('range', pot_name, formatted))
                            except (ValueError, IndexError) as e:
                                print(f"No se pudo parsear rango: '{line}'. Error: {e}")

                        self.serial_queue.put(('terminal', line))
            except serial.SerialException:
                self.serial_queue.put(('connection_lost',))
                break
            except Exception as e:
                print(f"Error leyendo serial: {e}")
            time.sleep(0.001)

    def _process_data_line(self, line):
        """Process a Pot data line (main thread)."""
        try:
            parts = line.replace('|', ',').split(',')
            for part in parts:
                part = part.strip()
                if ':' in part:
                    split_part = part.split(':')
                    if len(split_part) != 2:
                        continue
                    pot_name, value_str = [s.strip() for s in split_part]

                    if pot_name in self.pot_data:
                        pot_info = self.pot_data[pot_name]
                        raw_value = float(value_str)

                        try:
                            max_range = float(self.range_entries[pot_name].get())
                            processed_value = max_range - raw_value
                        except ValueError:
                            processed_value = raw_value

                        adjusted_value = processed_value - pot_info['offset']
                        current_time = time.time() - self.start_time

                        pot_info['values'].append(adjusted_value)
                        pot_info['times'].append(current_time)

                        if self.is_recording_session:
                            pot_info['all_values'].append(adjusted_value)
                            pot_info['all_times'].append(current_time)

                            if pot_info['min_session'] is None or adjusted_value < pot_info['min_session']:
                                pot_info['min_session'] = adjusted_value
                            if pot_info['max_session'] is None or adjusted_value > pot_info['max_session']:
                                pot_info['max_session'] = adjusted_value

                        # Update sensor label (single widget in monitor panel)
                        self.pot_labels[pot_name].config(
                            text=f"{adjusted_value:.2f} mm")
        except Exception as e:
            print(f"Error procesando datos: {e}")

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def update_ports(self):
        ports = [port.device for port in serial.tools.list_ports.comports()]
        self.port_combo['values'] = ports
        if ports:
            self.port_combo.current(0)

    def toggle_connection(self):
        if not self.is_reading:
            for pot_name in self.range_entries:
                self.range_entries[pot_name].delete(0, tk.END)
                self.range_entries[pot_name].insert(0, "...")
            self.connect()
        else:
            self.disconnect()

    def connect(self):
        port = self.port_combo.get()
        if not port:
            messagebox.showerror("Error", "Selecciona un puerto serial")
            return

        try:
            self.serial_conn = serial.Serial(port, 9600, timeout=1)
            time.sleep(2)
            self.is_reading = True
            self.last_port = port
            self.reconnect_attempts = 0
            self.is_reconnecting = False
            self.connect_btn.config(text="Desconectar", style='Disconnect.TButton')
            self.record_btn.config(state='normal')
            self.pause_btn.config(state='normal')
            self.port_label.config(text=port)

            self.read_thread = threading.Thread(target=self.read_serial, daemon=True)
            self.read_thread.start()

            self.update_plot()
            for i in range(1, 6):
                if self.pot_data[f'Pot{i}']['enabled']:
                    self.send_command(f'LON{i}\n')

            # NO auto-open calibration (manual per user decision)
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo conectar: {str(e)}")

    def disconnect(self):
        self.is_reading = False
        self.is_reconnecting = False
        if self.serial_conn:
            try:
                for i in range(1, 6):
                    self.send_command(f'LOFF{i}\n')
                time.sleep(0.1)
                self.serial_conn.close()
            except Exception:
                pass
            self.serial_conn = None
        self.connect_btn.config(text="Conectar", style='Connect.TButton')
        self.record_btn.config(state='disabled')
        self.pause_btn.config(state='disabled', text="Pausar")
        self.is_paused = False
        self.port_label.config(text="Sin conexión")

        if self.is_recording_session:
            self.is_recording_session = False
            self.recording_start_time = None
            self.record_btn.config(text="Iniciar captura", style='Record.TButton')

    def attempt_reconnect(self):
        """Start auto-reconnection sequence."""
        if self.is_reconnecting or not self.last_port:
            return

        self.is_reading = False
        self.is_reconnecting = True
        self.reconnect_attempts = 0

        if self.serial_conn:
            try:
                self.serial_conn.close()
            except Exception:
                pass
            self.serial_conn = None

        self._try_reconnect()

    def _try_reconnect(self):
        if not self.is_reconnecting:
            return

        self.reconnect_attempts += 1

        if self.reconnect_attempts > self.max_reconnect_attempts:
            self.is_reconnecting = False
            messagebox.showwarning("Conexión perdida",
                                   f"No se pudo reconectar después de {self.max_reconnect_attempts} intentos.")
            self.connect_btn.config(text="Conectar", style='Connect.TButton')
            self.record_btn.config(state='disabled')
            self.port_label.config(text="Sin conexión")
            return

        try:
            self.serial_conn = serial.Serial(self.last_port, 9600, timeout=1)
            time.sleep(1)
            self.is_reading = True
            self.is_reconnecting = False
            self.reconnect_attempts = 0

            self.read_thread = threading.Thread(target=self.read_serial, daemon=True)
            self.read_thread.start()
            self.update_plot()

            for i in range(1, 6):
                if self.pot_data[f'Pot{i}']['enabled']:
                    self.send_command(f'LON{i}\n')
        except Exception:
            self.root.after(self.reconnect_delay, self._try_reconnect)

    def send_command(self, command):
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self.serial_conn.write(command.encode('utf-8', errors='ignore'))
                return True
            except Exception as e:
                print(f"Error enviando comando: {e}")
                return False
        return False

    def send_calibration_command(self, cmd):
        if self.send_command(cmd + '\n'):
            if self.terminal_text:
                try:
                    self.terminal_text.insert(tk.END, f">>> {cmd}\n", "command_sent")
                    self.terminal_text.tag_config("command_sent", foreground="#3498db",
                                                  font=("Courier New", 10, "bold"))
                    self.terminal_text.see(tk.END)
                except tk.TclError:
                    pass

    # ------------------------------------------------------------------
    # Sensor controls
    # ------------------------------------------------------------------

    def toggle_pot(self, pot_name):
        is_enabled = self.pot_vars[pot_name].get()
        self.pot_data[pot_name]['enabled'] = is_enabled

        pot_index = int(pot_name.replace('Pot', ''))

        if is_enabled:
            self.send_command(f'E{pot_index}\n')
            self.send_command(f'LON{pot_index}\n')
        else:
            self.send_command(f'D{pot_index}\n')
            self.send_command(f'LOFF{pot_index}\n')

    def set_zero(self, pot_name):
        pot_info = self.pot_data[pot_name]
        btn = self.tare_buttons[pot_name]

        if not pot_info['is_tared']:
            if pot_info['values']:
                last_adjusted_value = list(pot_info['values'])[-1]
                pot_info['offset'] += last_adjusted_value
                pot_info['is_tared'] = True
                btn.config(text="Restaurar")
        else:
            pot_info['offset'] = 0
            pot_info['is_tared'] = False
            btn.config(text="Poner a 0")

    def set_transducer_range(self, pot_name, pot_index):
        new_range = self.range_entries[pot_name].get()
        if self.send_command(f'R{pot_index},{new_range}\n'):
            messagebox.showinfo("Comando Enviado",
                                f"Rango establecido a {new_range} mm.")

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def toggle_pause(self):
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.pause_btn.config(text="Reanudar", style='Disconnect.TButton')
        else:
            self.pause_btn.config(text="Pausar", style='Action.TButton')

    def toggle_recording(self):
        self.is_recording_session = not self.is_recording_session
        if self.is_recording_session:
            self.start_time = time.time()
            self.recording_start_time = time.time()
            self.session_start_time = datetime.now()

            for pot_info in self.pot_data.values():
                pot_info['values'].clear()
                pot_info['times'].clear()
                pot_info['all_values'].clear()
                pot_info['all_times'].clear()
                pot_info['min_session'] = None
                pot_info['max_session'] = None

            self.record_btn.config(text="Detener captura", style='Disconnect.TButton')
        else:
            self.recording_start_time = None
            self.record_btn.config(text="Iniciar captura", style='Record.TButton')
            self._refresh_summary_table()

        self._update_export_buttons()

    def _update_export_buttons(self):
        has_data = any(len(info['all_values']) > 0
                       for info in self.pot_data.values() if info['enabled'])
        state = 'normal' if has_data else 'disabled'
        self.csv_btn.config(state=state)
        self.pdf_btn.config(state=state)

    def _refresh_summary_table(self):
        pass  # Summary is now shown in CSV/PDF exports directly

    # ------------------------------------------------------------------
    # Plot update (every 100ms)
    # ------------------------------------------------------------------

    def update_plot(self):
        if not self.is_reading:
            return

        if self.is_paused:
            self.root.after(100, self.update_plot)
            return

        ax = self.ax_main
        all_y_data = []
        all_x_data = []

        for pot_name, pot_info in self.pot_data.items():
            line = self.lines[pot_name]
            if pot_info['enabled'] and pot_info['values']:
                x_data = list(pot_info['times'])
                y_data = list(pot_info['values'])
                line.set_data(x_data, y_data)
                line.set_visible(True)
                all_y_data.extend(y_data)
                all_x_data.extend(x_data)
            elif line.get_xdata() is not None and len(line.get_xdata()) > 0:
                # Keep last drawn data visible (line does NOT disappear)
                xd = line.get_xdata()
                yd = line.get_ydata()
                if len(xd) > 0:
                    all_x_data.extend(xd)
                    all_y_data.extend(yd)
            else:
                line.set_visible(False)

        if all_x_data:
            ax.set_xlim(0, max(10, max(all_x_data) * 1.05))

        if all_y_data:
            y_min, y_max = min(all_y_data), max(all_y_data)
            margin = (y_max - y_min) * 0.1
            if margin == 0:
                margin = 5.0
            ax.set_ylim(y_min - margin, y_max + margin)

        # Update min/max annotation
        min_session = None
        max_session = None
        for pot_info in self.pot_data.values():
            if pot_info['enabled'] and pot_info['min_session'] is not None:
                if min_session is None or pot_info['min_session'] < min_session:
                    min_session = pot_info['min_session']
            if pot_info['enabled'] and pot_info['max_session'] is not None:
                if max_session is None or pot_info['max_session'] > max_session:
                    max_session = pot_info['max_session']

        if min_session is not None and max_session is not None:
            self.min_max_text.set_text(f'Min: {min_session:.2f} | Max: {max_session:.2f}')
        else:
            self.min_max_text.set_text('')

        self.canvas_main.draw_idle()
        self.root.after(100, self.update_plot)

    # ------------------------------------------------------------------
    # Calibration popup (redesigned)
    # ------------------------------------------------------------------

    def show_calibration_popup(self):
        popup = tk.Toplevel(self.root)
        popup.title("Calibración")
        popup.geometry("780x440")
        popup.transient(self.root)
        popup.grab_set()

        popup.columnconfigure(0, weight=1)
        popup.rowconfigure(0, weight=0)  # sensor cards
        popup.rowconfigure(1, weight=1)  # terminal
        popup.rowconfigure(2, weight=0)  # command bar
        popup.rowconfigure(3, weight=0)  # bottom buttons

        # Calibration state: pending / calibrating / done / disabled
        cal_state = {}
        in_cal_mode = [False]  # mutable flag: whether firmware is in calibration menu

        for i in range(1, 6):
            cal_state[i] = 'disabled' if not self.pot_data[f'Pot{i}']['enabled'] else 'pending'

        # ---- Sensor cards ----
        cards_frame = tk.Frame(popup)
        cards_frame.grid(row=0, column=0, sticky='ew', padx=5, pady=5)
        for col in range(5):
            cards_frame.columnconfigure(col, weight=1)

        card_labels = {}
        card_status_labels = {}
        card_buttons = {}

        for i in range(5):
            pot_name = f'Pot{i+1}'
            pot_info = self.pot_data[pot_name]
            color = pot_info['color']
            enabled = pot_info['enabled']

            card = tk.Frame(cards_frame, bg=color, relief='raised', bd=1)
            card.grid(row=0, column=i, sticky='nsew', padx=2)
            card.columnconfigure(0, weight=1)

            tk.Label(card, text=f"Sensor {i+1}", font=self.font_bold,
                     fg='white', bg=color).grid(row=0, column=0, pady=(4, 0))

            val_text = "Desactivado" if not enabled else "---.---"
            if enabled and pot_info['values']:
                val_text = f"{list(pot_info['values'])[-1]:.2f} mm"
            val_lbl = tk.Label(card, text=val_text, font=self.font_normal,
                               fg='white', bg=color)
            val_lbl.grid(row=1, column=0, pady=2)
            card_labels[i+1] = val_lbl

            status_text = "---" if not enabled else "Pendiente"
            status_lbl = tk.Label(card, text=status_text, font=self.font_small,
                                  fg='#ecf0f1', bg=color)
            status_lbl.grid(row=2, column=0, pady=2)
            card_status_labels[i+1] = status_lbl

            btn_text = "Calibrar" if enabled else "------"
            btn_state = 'normal' if enabled else 'disabled'
            cal_btn = ttk.Button(card, text=btn_text, state=btn_state,
                                 style='Small.TButton')
            cal_btn.grid(row=3, column=0, pady=(2, 6), padx=8, sticky='ew')
            card_buttons[i+1] = cal_btn

        # ---- Terminal ----
        term_frame = tk.Frame(popup)
        term_frame.grid(row=1, column=0, sticky='nsew', padx=5, pady=2)
        term_frame.columnconfigure(0, weight=1)
        term_frame.rowconfigure(0, weight=1)

        self.terminal_text = tk.Text(term_frame, wrap=tk.WORD, height=5,
                                     font=self.font_mono, bg="#2c3e50",
                                     fg="#ecf0f1", insertbackground="white")
        self.terminal_text.grid(row=0, column=0, sticky='nsew')

        scrollbar = ttk.Scrollbar(term_frame, command=self.terminal_text.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.terminal_text.config(yscrollcommand=scrollbar.set)

        # ---- Command bar ----
        cmd_frame = tk.Frame(popup)
        cmd_frame.grid(row=2, column=0, sticky='ew', padx=5, pady=2)
        cmd_frame.columnconfigure(0, weight=1)

        cmd_entry = ttk.Entry(cmd_frame, font=self.font_normal)
        cmd_entry.grid(row=0, column=0, sticky='ew', padx=(0, 4))

        def send_entry_cmd(event=None):
            cmd = cmd_entry.get()
            if cmd:
                self.send_calibration_command(cmd)
                cmd_entry.delete(0, tk.END)

        cmd_entry.bind('<Return>', send_entry_cmd)

        ttk.Button(cmd_frame, text="ENVIAR", style='Small.TButton',
                   command=send_entry_cmd).grid(row=0, column=1, padx=2)
        ttk.Button(cmd_frame, text="ENTER", style='Small.TButton',
                   command=lambda: self.send_calibration_command('')).grid(
            row=0, column=2, padx=2)

        # ---- Bottom buttons ----
        bottom = tk.Frame(popup)
        bottom.grid(row=3, column=0, sticky='ew', padx=5, pady=5)
        bottom.columnconfigure(1, weight=1)

        def save_and_exit():
            self.send_calibration_command('S')
            in_cal_mode[0] = False
            popup.after(300, cleanup_and_close)

        def cleanup_and_close():
            self.terminal_text = None
            popup.destroy()

        def close_popup():
            active_cal = any(cal_state[j] == 'calibrating' for j in range(1, 6))
            if active_cal or in_cal_mode[0]:
                if messagebox.askyesno(
                        "Calibración en progreso",
                        "Hay calibración en progreso. ¿Enviar 'S' (guardar) antes de cerrar?",
                        parent=popup):
                    save_and_exit()
                    return
            cleanup_and_close()

        ttk.Button(bottom, text="GUARDAR Y SALIR (S)", style='Connect.TButton',
                   command=save_and_exit).grid(row=0, column=0, padx=5, sticky='w', ipady=4)
        ttk.Button(bottom, text="CERRAR", style='Disconnect.TButton',
                   command=close_popup).grid(row=0, column=2, padx=5, sticky='e', ipady=4)

        # ---- Calibration flow per sensor ----
        def start_calibration(sensor_idx):
            cal_state[sensor_idx] = 'calibrating'
            card_status_labels[sensor_idx].config(text="Calibrando...")
            card_buttons[sensor_idx].config(state='disabled')

            if not in_cal_mode[0]:
                # First time: send LBLINK + C to enter calibration mode
                self.send_command(f'LBLINK{sensor_idx}\n')
                self.send_calibration_command('C')
                in_cal_mode[0] = True
                popup.after(500, lambda: self.send_calibration_command(str(sensor_idx)))
            else:
                # Already in cal menu, just send digit
                self.send_calibration_command(str(sensor_idx))

            # After user completes two ENTERs, they click "Marcar Listo"
            def mark_done():
                cal_state[sensor_idx] = 'done'
                card_status_labels[sensor_idx].config(text="Listo ✓")
                self.send_command(f'LON{sensor_idx}\n')
                card_buttons[sensor_idx].config(text="Calibrar", state='normal',
                                                command=lambda: start_calibration(sensor_idx))

            card_buttons[sensor_idx].config(text="Marcar Listo", state='normal',
                                            command=mark_done)

        for i in range(1, 6):
            if cal_state[i] != 'disabled':
                card_buttons[i].config(command=lambda idx=i: start_calibration(idx))

        # ---- Periodic card value update ----
        def update_cards():
            if not popup.winfo_exists():
                return
            for i in range(1, 6):
                pot_name = f'Pot{i}'
                pot_info = self.pot_data[pot_name]
                if pot_info['enabled'] and pot_info['values']:
                    card_labels[i].config(text=f"{list(pot_info['values'])[-1]:.2f} mm")
            popup.after(500, update_cards)

        update_cards()

    # ------------------------------------------------------------------
    # Export CSV
    # ------------------------------------------------------------------

    def export_csv(self):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        default_name = f"datos_transductores_{timestamp}.csv"
        filename = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=default_name,
        )

        if not filename:
            return

        try:
            file_path = Path(filename)
            with open(file_path, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)

                # Session timestamp header
                if self.session_start_time:
                    writer.writerow([f"Sesión iniciada: {self.session_start_time.strftime('%Y-%m-%d %H:%M:%S')}"])
                    writer.writerow([f"Exportado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"])
                    writer.writerow([])

                headers = ['Tiempo (s)']
                enabled_pots = []

                for pot_name, pot_info in self.pot_data.items():
                    if pot_info['enabled']:
                        headers.append(pot_name.replace('Pot', 'Sensor '))
                        enabled_pots.append(pot_name)

                writer.writerow(headers)

                if not enabled_pots:
                    messagebox.showwarning("Advertencia",
                                           "No hay transductores habilitados para exportar.")
                    return

                first_pot_info = self.pot_data[enabled_pots[0]]
                max_len = len(first_pot_info['all_values'])

                for i in range(max_len):
                    row = [f"{first_pot_info['all_times'][i]:.4f}"]
                    for pot_name in enabled_pots:
                        pot_info = self.pot_data[pot_name]
                        if i < len(pot_info['all_values']):
                            row.append(f"{pot_info['all_values'][i]:.4f}")
                        else:
                            row.append('')
                    writer.writerow(row)

            messagebox.showinfo("Exportado", f"Datos exportados:\n{filename}")
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo exportar: {str(e)}")

    # ------------------------------------------------------------------
    # Export PDF
    # ------------------------------------------------------------------

    def generate_pdf_report(self):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        default_name = f"reporte_transductores_{timestamp}.pdf"
        filename = filedialog.asksaveasfilename(
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
            initialfile=default_name,
        )

        if not filename:
            return

        try:
            file_path = Path(filename)
            doc = SimpleDocTemplate(str(file_path), pagesize=letter)
            elements = []
            styles = getSampleStyleSheet()

            title = Paragraph("Reporte de Monitoreo de Transductores", styles['Title'])
            elements.append(title)
            elements.append(Spacer(1, 12))

            date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            elements.append(Paragraph(f"<b>Fecha de generación:</b> {date_str}", styles['Normal']))

            if self.session_start_time:
                elements.append(Paragraph(
                    f"<b>Sesión iniciada:</b> {self.session_start_time.strftime('%Y-%m-%d %H:%M:%S')}",
                    styles['Normal']))

            elements.append(Spacer(1, 8))

            enabled_list = [pot.replace('Pot', 'Sensor ')
                            for pot, info in self.pot_data.items() if info['enabled']]
            elements.append(Paragraph(
                f"<b>Transductores monitoreados:</b> {', '.join(enabled_list)}",
                styles['Normal']))
            elements.append(Spacer(1, 20))

            table_data = [['Transductor', 'Valor Actual', 'Promedio', 'Mínimo', 'Máximo', 'Muestras']]

            for pot_name, pot_info in self.pot_data.items():
                if pot_info['enabled'] and pot_info['all_values']:
                    data = pot_info['all_values']
                    avg = sum(data) / len(data)
                    table_data.append([
                        pot_name.replace('Pot', 'Sensor '),
                        f"{data[-1]:.4f}",
                        f"{avg:.4f}",
                        f"{min(data):.4f}",
                        f"{max(data):.4f}",
                        str(len(data)),
                    ])

            table = Table(table_data)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#34495e')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 11),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('TOPPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#ecf0f1')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#ecf0f1')]),
                ('GRID', (0, 0), (-1, -1), 1, colors.grey),
            ]))

            elements.append(table)
            elements.append(Spacer(1, 25))

            elements.append(Paragraph("Gráfica General", styles['Heading2']))
            elements.append(Spacer(1, 15))

            fig_general = Figure(figsize=(10, 4))
            ax_general = fig_general.add_subplot(111)
            ax_general.set_xlabel('Tiempo (s)', fontsize=10)
            ax_general.set_ylabel('Valor (mm)', fontsize=10)
            ax_general.set_title("Monitoreo en Tiempo Real - Todos los Sensores",
                                 fontsize=12, fontweight='bold')
            ax_general.grid(True, alpha=0.4, linestyle='--')
            ax_general.tick_params(labelsize=9)
            ax_general.yaxis.set_major_formatter(FormatStrFormatter('%.4f'))

            for pot_name, pot_info in self.pot_data.items():
                if pot_info['enabled'] and pot_info['all_values']:
                    ax_general.plot(pot_info['all_times'], pot_info['all_values'],
                                    color=pot_info['color'], linewidth=2,
                                    label=pot_name.replace('Pot', 'S'))

            ax_general.legend(loc='upper left', fontsize=9)

            import tempfile
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_img = str(Path(temp_dir) / f"temp_general_{int(time.time())}.png")
                fig_general.savefig(temp_img, dpi=150, bbox_inches='tight')

                elements.append(Image(temp_img, width=460, height=230))
                elements.append(Spacer(1, 15))

                doc.build(elements)

            messagebox.showinfo("Reporte Generado", f"PDF generado:\n{filename}")
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo generar el reporte: {str(e)}")

    # ------------------------------------------------------------------
    # Close confirmation
    # ------------------------------------------------------------------

    def on_closing(self):
        if self.is_recording_session:
            if not messagebox.askyesno("Confirmar cierre",
                                        "Hay captura en progreso. ¿Deseas cerrar de todas formas?"):
                return

        if self.is_reading:
            self.disconnect()

        self.root.destroy()


def main():
    root = tk.Tk()
    app = ArduinoMonitor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
