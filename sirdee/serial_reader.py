"""Threaded serial transport for the Arduino monitor."""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

import serial
import serial.tools.list_ports

from .constants import BAUDRATE, NUM_SENSORS
from .sensors import parse_sensor_line


class SerialReader:
    """Read complete serial lines without touching Tk from the worker thread."""

    def __init__(self, sensor_count: int = NUM_SENSORS) -> None:
        self.sensor_count = sensor_count
        self.connection: Optional[serial.Serial] = None
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.lines: queue.Queue[str] = queue.Queue(maxsize=1000)
        self.connected = False

    @staticmethod
    def ports() -> list[str]:
        return [item.device for item in serial.tools.list_ports.comports()]

    def connect(self, port: str, baudrate: int = BAUDRATE) -> bool:
        try:
            self.connection = serial.Serial(port, baudrate, timeout=1)
            time.sleep(2)
            self.stop_event.clear()
            self.connected = True
            self.thread = threading.Thread(target=self._read_loop, daemon=True)
            self.thread.start()
            return True
        except (serial.SerialException, OSError) as exc:
            print(f"Error al conectar: {exc}")
            self.connection = None
            self.connected = False
            return False

    def disconnect(self) -> None:
        self.stop_event.set()
        self.connected = False
        if self.connection is not None:
            try:
                self.connection.close()
            except (serial.SerialException, OSError):
                pass
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.5)
        self.connection = None
        self.thread = None

    def send(self, command: str) -> bool:
        if not self.connected or self.connection is None or not self.connection.is_open:
            return False
        try:
            payload = f"{command.rstrip(chr(10))}\n".encode("utf-8", errors="ignore")
            self.connection.write(payload)
            return True
        except (serial.SerialException, OSError) as exc:
            print(f"Error enviando comando: {exc}")
            return False

    def get_line(self) -> Optional[str]:
        try:
            return self.lines.get_nowait()
        except queue.Empty:
            return None

    def parse_line(self, line: str) -> tuple[list[Optional[float]], bool] | None:
        values = parse_sensor_line(line, self.sensor_count)
        return (values, True) if values is not None else None

    def _read_loop(self) -> None:
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
