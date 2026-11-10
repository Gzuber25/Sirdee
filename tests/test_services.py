import csv
import tempfile
import unittest
from pathlib import Path

from sirdee.reports import export_csv
from sirdee.sensors import parse_sensor_line, sensor_state


class SensorParsingTests(unittest.TestCase):
    def test_parses_pipe_and_comma_separated_values(self):
        self.assertEqual(
            parse_sensor_line("Pot1: 25.43|Pot2: 24.91, Pot5: 24.88"),
            [25.43, 24.91, None, None, 24.88],
        )

    def test_ignores_non_sensor_lines_and_bad_values(self):
        self.assertIsNone(parse_sensor_line("status: ready"))
        self.assertEqual(parse_sensor_line("Pot1: invalid, Pot2: 1.5"), [None, 1.5, None, None, None])

    def test_sensor_state_thresholds(self):
        self.assertEqual(sensor_state(12.5, 25.0)[0], "Normal")
        self.assertEqual(sensor_state(1.0, 25.0)[0], "Advertencia")
        self.assertEqual(sensor_state(26.0, 25.0)[0], "Fuera de rango")


class ReportTests(unittest.TestCase):
    def test_csv_export_uses_recorded_sensor_data(self):
        sensors = {
            "Pot1": {"enabled": True, "all_times": [0.0, 0.1], "all_values": [1.25, 2.5]},
            "Pot2": {"enabled": False, "all_times": [], "all_values": []},
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capture.csv"
            export_csv(path, sensors)
            with path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.reader(stream))

        self.assertEqual(rows[0], ["Tiempo (s)", "Sensor 1"])
        self.assertEqual(rows[1], ["0.0000", "1.2500"])
        self.assertEqual(rows[2], ["0.1000", "2.5000"])


if __name__ == "__main__":
    unittest.main()
