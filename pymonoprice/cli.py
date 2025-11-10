"""Command-line helper for controlling Monoprice amplifiers."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from pymonoprice import ZoneStatus, get_monoprice

BOOL_TRUE = {"true", "on", "yes", "1"}
BOOL_FALSE = {"false", "off", "no", "0"}
FIELD_SETTERS = {
    "power": "set_power",
    "mute": "set_mute",
    "volume": "set_volume",
    "treble": "set_treble",
    "bass": "set_bass",
    "balance": "set_balance",
    "source": "set_source",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pymonoctl",
        description="Control Monoprice multi-zone amplifiers using pymonoprice",
    )
    conn = parser.add_argument_group("connection")
    conn.add_argument("--port", help="Serial device or pyserial URL (e.g. /dev/ttyUSB0)")
    conn.add_argument("--host", help="Host running a serial bridge; implies socket:// URL")
    conn.add_argument("--tcp-port", type=int, default=23, help="TCP port when using --host")
    conn.add_argument(
        "--model",
        default="auto",
        choices=["auto", "10761", "31028"],
        help="Force a specific Monoprice model",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Read zone status")
    status.add_argument("--zone", type=int, action="append", help="Specific zone number (11-16, etc)")
    status.add_argument("--unit", type=int, default=1, help="Unit number when --zone is omitted")
    status.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    set_cmd = sub.add_parser("set", help="Modify zone attributes")
    set_cmd.add_argument("--zone", type=int, required=True, help="Zone to modify")
    set_cmd.add_argument(
        "assignments",
        nargs="+",
        metavar="KEY=VALUE",
        help="Assignments such as power=on volume=15",
    )

    apply_cmd = sub.add_parser("apply-config", help="Apply a JSON/YAML config file")
    apply_cmd.add_argument("config", help="Path to configuration file containing a 'zones' mapping")

    return parser


def _resolve_port_url(args: argparse.Namespace) -> str:
    if args.port:
        return args.port
    if args.host:
        return f"socket://{args.host}:{args.tcp_port}"
    raise ValueError("Specify --port or --host")


def _coerce_value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered in BOOL_TRUE:
        return True
    if lowered in BOOL_FALSE:
        return False
    if lowered == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _parse_assignments(assignments: Iterable[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for item in assignments:
        if "=" not in item:
            raise ValueError(f"Invalid assignment '{item}', expected KEY=VALUE")
        key, raw = item.split("=", 1)
        key = key.strip().lower()
        if not key:
            raise ValueError(f"Missing key in assignment '{item}'")
        result[key] = _coerce_value(raw)
    return result


def _apply_zone_settings(device: Any, zone: int, settings: Dict[str, Any]) -> None:
    for key, value in settings.items():
        setter_name = FIELD_SETTERS.get(key)
        if not setter_name:
            raise ValueError(f"Unsupported attribute '{key}'")
        setter = getattr(device, setter_name, None)
        if not setter:
            raise ValueError(f"Device does not support '{key}'")
        setter(zone, value)


def _load_config(path: Path) -> Dict[str, Any]:
    text = path.read_text()
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("PyYAML is required to parse YAML files") from exc
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Configuration file must contain a mapping at the top level")
    return data


def _command_status(device: Any, args: argparse.Namespace) -> int:
    zones: List[ZoneStatus] = []
    if args.zone:
        for zone in args.zone:
            status = device.zone_status(zone)
            if status:
                zones.append(status)
    else:
        zones.extend(device.all_zone_status(args.unit))
    if args.json:
        print(json.dumps([asdict(zone) for zone in zones], indent=2))
        return 0
    for zone in zones:
        print(f"Zone {zone.zone}:")
        print(f"  Power: {'on' if zone.power else 'off'}")
        print(f"  Mute: {'on' if zone.mute else 'off'}")
        print(f"  Volume: {zone.volume}")
        print(f"  Treble: {zone.treble}")
        print(f"  Bass: {zone.bass}")
        print(f"  Balance: {zone.balance}")
        print(f"  Source: {zone.source}")
        print(f"  Keypad: {'connected' if zone.keypad else 'not connected'}")
    return 0


def _command_set(device: Any, args: argparse.Namespace) -> int:
    settings = _parse_assignments(args.assignments)
    _apply_zone_settings(device, args.zone, settings)
    return 0


def _command_apply_config(device: Any, args: argparse.Namespace) -> int:
    config = _load_config(Path(args.config))
    zones_section = config.get("zones")
    if not isinstance(zones_section, dict):
        raise ValueError("Configuration file must contain a 'zones' mapping")
    for zone_key, settings in zones_section.items():
        zone = int(zone_key)
        if not isinstance(settings, dict):
            raise ValueError(f"Zone {zone_key} settings must be a mapping")
        normalized = {k.lower(): v if not isinstance(v, str) else _coerce_value(v) for k, v in settings.items()}
        _apply_zone_settings(device, zone, normalized)
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        port_url = _resolve_port_url(args)
        model = None if args.model == "auto" else args.model
        device = get_monoprice(port_url, model=model)
    except Exception as exc:  # noqa: BLE001
        print(f"Connection error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "status":
            return _command_status(device, args)
        if args.command == "set":
            return _command_set(device, args)
        if args.command == "apply-config":
            return _command_apply_config(device, args)
        parser.error("Unknown command")
    finally:
        port = getattr(device, "_port", None)
        if port:
            try:
                port.close()
            except Exception:  # noqa: BLE001
                pass
if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
