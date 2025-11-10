import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pymonoprice import ZoneStatus
from pymonoprice import cli


class DummyPort:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class DummyDevice:
    def __init__(self, zone_status: ZoneStatus | None = None) -> None:
        self._port = DummyPort()
        self._status = zone_status or ZoneStatus(11, False, True, False, False, 10, 7, 7, 10, 1, False)
        self.set_calls: list[tuple[str, int, object]] = []

    def zone_status(self, zone: int) -> ZoneStatus:
        return self._status

    def all_zone_status(self, unit: int):
        return [self._status]

    def set_power(self, zone: int, value):
        self.set_calls.append(("power", zone, value))

    def set_volume(self, zone: int, value):
        self.set_calls.append(("volume", zone, value))

    def set_balance(self, zone: int, value):
        self.set_calls.append(("balance", zone, value))

    def set_mute(self, zone: int, value):
        self.set_calls.append(("mute", zone, value))

    def set_treble(self, zone: int, value):
        self.set_calls.append(("treble", zone, value))

    def set_bass(self, zone: int, value):
        self.set_calls.append(("bass", zone, value))

    def set_source(self, zone: int, value):
        self.set_calls.append(("source", zone, value))


class CLITests(unittest.TestCase):
    def test_status_json(self):
        device = DummyDevice()
        with patch("pymonoprice.cli.get_monoprice", return_value=device) as mock_get:
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = cli.main(["--port", "/dev/test", "status", "--zone", "11", "--json"])
        self.assertEqual(rc, 0)
        self.assertIn("\"zone\": 11", buf.getvalue())
        mock_get.assert_called_once_with("/dev/test", model=None)
        self.assertTrue(device._port.closed)

    def test_set_assignments(self):
        device = DummyDevice()
        with patch("pymonoprice.cli.get_monoprice", return_value=device):
            rc = cli.main(["--port", "/dev/test", "set", "--zone", "11", "power=on", "volume=15"])
        self.assertEqual(rc, 0)
        self.assertIn(("power", 11, True), device.set_calls)
        self.assertIn(("volume", 11, 15), device.set_calls)

    def test_apply_config(self):
        device = DummyDevice()
        config = {"zones": {"11": {"power": "on", "balance": 12}}}
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            json.dump(config, tmp)
            tmp_path = Path(tmp.name)
        try:
            with patch("pymonoprice.cli.get_monoprice", return_value=device):
                rc = cli.main(["--port", "/dev/test", "apply-config", str(tmp_path)])
        finally:
            tmp_path.unlink(missing_ok=True)
        self.assertEqual(rc, 0)
        self.assertIn(("power", 11, True), device.set_calls)
        self.assertIn(("balance", 11, 12), device.set_calls)

    def test_socket_connection(self):
        device = DummyDevice()
        with patch("pymonoprice.cli.get_monoprice", return_value=device) as mock_get:
            cli.main(["--host", "192.0.2.10", "--tcp-port", "5000", "status", "--unit", "1", "--json"])
        mock_get.assert_called_once_with("socket://192.0.2.10:5000", model=None)

if __name__ == "__main__":
    unittest.main()
