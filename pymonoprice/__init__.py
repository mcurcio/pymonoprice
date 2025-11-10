from __future__ import annotations

import asyncio
import logging
import re
import serial
from dataclasses import dataclass
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from serial_asyncio_fast import create_serial_connection, SerialTransport
from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Awaitable, Callable, Concatenate, ParamSpec, TypeVar

    _P = ParamSpec("_P")
    _T = TypeVar("_T")
    _AsyncLockable = TypeVar("_AsyncLockable", "MonopriceAsync", "MonopriceProtocol")

_LOGGER = logging.getLogger(__name__)
ZONE_PATTERN = re.compile(
    r">(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)(\d\d)"
)

EOL = b"\r\n#"
LEN_EOL = len(EOL)
TIMEOUT = 2  # Number of seconds before serial operation timeout
MODEL_10761 = "10761"
MODEL_31028 = "31028"


def synchronized(
    func: Callable[Concatenate[Monoprice, _P], _T]
) -> Callable[Concatenate[Monoprice, _P], _T]:
    @wraps(func)
    def wrapper(self: Monoprice, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        with self._lock:
            return func(self, *args, **kwargs)

    return wrapper


def locked_coro(
    coro: Callable[Concatenate[_AsyncLockable, _P], Awaitable[_T]]
) -> Callable[Concatenate[_AsyncLockable, _P], Awaitable[_T]]:
    @wraps(coro)
    async def wrapper(self: _AsyncLockable, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        async with self._lock:
            return await coro(self, *args, **kwargs)  # type: ignore[return-value]

    return wrapper


def connected(
    coro: Callable[Concatenate[MonopriceProtocol, _P], Awaitable[_T]]
) -> Callable[Concatenate[MonopriceProtocol, _P], Awaitable[_T]]:
    @wraps(coro)
    async def wrapper(
        self: MonopriceProtocol, *args: _P.args, **kwargs: _P.kwargs
    ) -> _T:
        await self._connected.wait()
        return await coro(self, *args, **kwargs)

    return wrapper


@dataclass
class ZoneStatus:
    zone: int
    pa: bool
    power: bool
    mute: bool
    do_not_disturb: bool
    volume: int  # 0 - 38
    treble: int  # 0 -> -7,  14-> +7
    bass: int  # 0 -> -7,  14-> +7
    balance: int  # 00 - left, 10 - center, 20 right
    source: int
    keypad: bool

    @classmethod
    def from_strings(cls, strings: list[str]) -> list[ZoneStatus]:
        if not strings:
            return list()
        return [zone for zone in (ZoneStatus.from_string(s) for s in strings) if zone is not None]

    @classmethod
    def from_string(cls, string: str) -> ZoneStatus | None:
        if not string:
            return None
        match = re.search(ZONE_PATTERN, string)
        if not match:
            return None
        (
            zone,
            pa,
            power,
            mute,
            do_not_disturb,
            volume,
            treble,
            bass,
            balance,
            source,
            keypad,
        ) = map(int, match.groups())
        return ZoneStatus(
            zone,
            bool(pa),
            bool(power),
            bool(mute),
            bool(do_not_disturb),
            volume,
            treble,
            bass,
            balance,
            source,
            bool(keypad),
        )


class Monoprice:
    def __init__(self, port_url: str, lock: RLock) -> None:
        """
        Monoprice amplifier interface
        """
        self._lock = lock
        self._port = serial.serial_for_url(port_url, do_not_open=True)
        _configure_serial_port(self._port)
        self._port.open()

    def _send_request(self, request: bytes) -> None:
        """
        :param request: request that is sent to the monoprice
        """
        _LOGGER.debug('Sending "%s"', request)
        # clear
        self._port.reset_output_buffer()
        self._port.reset_input_buffer()
        # send
        self._port.write(request)
        self._port.flush()

    def _process_request(self, request: bytes, num_eols_to_read: int = 1) -> str:
        """
        :param request: request that is sent to the monoprice
        :param num_eols_to_read: number of EOL sequences to read. When last EOL is read, reading stops
        :return: ascii string returned by monoprice
        """
        self._send_request(request)
        # receive
        result = bytearray()
        count = None
        while True:
            c = self._port.read(1)
            if not c:
                raise serial.SerialTimeoutException(
                    "Connection timed out! Last received bytes {}".format(
                        [hex(a) for a in result]
                    )
                )
            result += c
            count = _subsequence_count(result, EOL, count)
            if count[1] >= num_eols_to_read:
                break
        ret = bytes(result)
        _LOGGER.debug('Received "%s"', ret)
        return ret.decode("ascii")

    @synchronized
    def zone_status(self, zone: int) -> ZoneStatus | None:
        """
        Get the structure representing the status of the zone
        :param zone: zone 11..16, 21..26, 31..36
        :return: status of the zone or None
        """
        # Reading two lines as the response is in the form \r\n#>110001000010111210040\r\n#
        return ZoneStatus.from_string(
            self._process_request(_format_zone_status_request(zone), num_eols_to_read=2)
        )

    @synchronized
    def all_zone_status(self, unit: int) -> list[ZoneStatus]:
        """
        Get the structure representing the status of all zones in a unit
        :param unit: 1, 2, 3
        :return: list of all statuses of the unit's zones or empty list if unit number is incorrect
        """
        if unit < 1 or unit > 3:
            return []
        # Reading 7 lines, since response starts with EOL and each zone's status is followed by EOL
        response = self._process_request(
            _format_all_zones_status_request(unit), num_eols_to_read=7
        )
        return ZoneStatus.from_strings(response.split(sep=EOL.decode('ascii')))

    @synchronized
    def set_power(self, zone: int, power: bool) -> None:
        """
        Turn zone on or off
        :param zone: zone 11..16, 21..26, 31..36
        :param power: True to turn on, False to turn off
        """
        self._process_request(_format_set_power(zone, power))

    @synchronized
    def set_mute(self, zone: int, mute: bool) -> None:
        """
        Mute zone on or off
        :param zone: zone 11..16, 21..26, 31..36
        :param mute: True to mute, False to unmute
        """
        self._process_request(_format_set_mute(zone, mute))

    @synchronized
    def set_volume(self, zone: int, volume: int) -> None:
        """
        Set volume for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param volume: integer from 0 to 38 inclusive
        """
        self._process_request(_format_set_volume(zone, volume))

    @synchronized
    def set_treble(self, zone: int, treble: int) -> None:
        """
        Set treble for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param treble: integer from 0 to 14 inclusive, where 0 is -7 treble and 14 is +7
        """
        self._process_request(_format_set_treble(zone, treble))

    @synchronized
    def set_bass(self, zone: int, bass: int) -> None:
        """
        Set bass for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param bass: integer from 0 to 14 inclusive, where 0 is -7 bass and 14 is +7
        """
        self._process_request(_format_set_bass(zone, bass))

    @synchronized
    def set_balance(self, zone: int, balance: int) -> None:
        """
        Set balance for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param balance: integer from 0 to 20 inclusive, where 0 is -10(left), 0 is center and 20 is +10 (right)
        """
        self._process_request(_format_set_balance(zone, balance))

    @synchronized
    def set_source(self, zone: int, source: int) -> None:
        """
        Set source for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param source: integer from 0 to 6 inclusive
        """
        self._process_request(_format_set_source(zone, source))

    @synchronized
    def restore_zone(self, status: ZoneStatus) -> None:
        """
        Restores zone to it's previous state
        :param status: zone state to restore
        """
        self.set_power(status.zone, status.power)
        self.set_mute(status.zone, status.mute)
        self.set_volume(status.zone, status.volume)
        self.set_treble(status.zone, status.treble)
        self.set_bass(status.zone, status.bass)
        self.set_balance(status.zone, status.balance)
        self.set_source(status.zone, status.source)


PLUS_TERMINATOR = b"+"
ZONE_STATUS_RE_31028 = re.compile(r"#(?P<zone>\d)ZS\s+VO(?P<volume>\d+)\s+PO(?P<power>\d)\s+MU(?P<mute>\d)\s+IS(?P<input>\d+)")
BALANCE_RE_31028 = re.compile(r"\?(?P<zone>\d)BA(?P<balance>\d+)")


class Monoprice31028:
    def __init__(self, port_url: str, lock: RLock) -> None:
        """Monoprice 31028 amplifier interface."""

        self._lock = lock
        self._port = serial.serial_for_url(port_url, do_not_open=True)
        _configure_serial_port(self._port)
        self._port.open()

    def _send_request(self, request: bytes) -> None:
        _LOGGER.debug('Sending "%s"', request)
        self._port.reset_output_buffer()
        self._port.reset_input_buffer()
        self._port.write(request)
        self._port.flush()

    def _process_request(self, request: bytes) -> str:
        self._send_request(request)
        result = bytearray()
        while True:
            c = self._port.read(1)
            if not c:
                raise serial.SerialTimeoutException(
                    "Connection timed out! Last received bytes {}".format(
                        [hex(a) for a in result]
                    )
                )
            if c == PLUS_TERMINATOR:
                break
            result += c
        ret = bytes(result)
        _LOGGER.debug('Received "%s"', ret)
        return ret.decode("ascii")

    @staticmethod
    def _zone_to_wire(zone: int) -> int:
        if zone >= 10:
            zone = zone % 10
        if zone < 1 or zone > 6:
            raise ValueError(f"Invalid zone: {zone}")
        return zone

    @staticmethod
    def _canonical_zone(zone: int) -> int:
        return 10 + zone

    @staticmethod
    def _balance_wire_to_internal(balance: int) -> int:
        balance = max(0, min(balance, 63))
        return int(round(balance / 63 * 20))

    @staticmethod
    def _balance_internal_to_wire(balance: int) -> int:
        balance = max(0, min(balance, 20))
        return int(round(balance / 20 * 63))

    @staticmethod
    def _bool_to_flag(value: bool | int | str | None) -> str:
        return "1" if bool(value) else "0"

    def _query_zone(self, zone: int) -> tuple[int, int, int, int, int] | None:
        wire_zone = self._zone_to_wire(zone)
        response = self._process_request(f"?{wire_zone}ZS+".encode())
        match = ZONE_STATUS_RE_31028.match(response.strip())
        if not match:
            return None
        return (
            int(match.group("zone")),
            int(match.group("volume")),
            int(match.group("power")),
            int(match.group("mute")),
            int(match.group("input")),
        )

    def _query_balance(self, zone: int) -> int | None:
        wire_zone = self._zone_to_wire(zone)
        response = self._process_request(f"?{wire_zone}BA+".encode())
        match = BALANCE_RE_31028.match(response.strip())
        if not match:
            return None
        return int(match.group("balance"))

    @synchronized
    def zone_status(self, zone: int) -> ZoneStatus | None:
        status = self._query_zone(zone)
        balance = self._query_balance(zone)
        if not status or balance is None:
            return None

        _, volume, power, mute, source = status
        canonical_zone = self._canonical_zone(self._zone_to_wire(zone))
        return ZoneStatus(
            canonical_zone,
            False,
            bool(power),
            bool(mute),
            False,
            int(max(0, min(volume, 38))),
            7,
            7,
            self._balance_wire_to_internal(balance),
            source,
            False,
        )

    @synchronized
    def all_zone_status(self, unit: int) -> list[ZoneStatus]:
        if unit != 1:
            return []
        statuses: list[ZoneStatus] = []
        for zone in range(11, 17):
            status = self.zone_status(zone)
            if status:
                statuses.append(status)
        return statuses

    def _write_command(self, zone: int, command: str, value: str) -> None:
        wire_zone = self._zone_to_wire(zone)
        payload = f"!{wire_zone}{command}{value}+".encode()
        self._send_request(payload)

    @synchronized
    def set_power(self, zone: int, power: bool) -> None:
        self._write_command(zone, "PR", self._bool_to_flag(power))

    @synchronized
    def set_mute(self, zone: int, mute: bool) -> None:
        self._write_command(zone, "MU", self._bool_to_flag(mute))

    @synchronized
    def set_volume(self, zone: int, volume: int) -> None:
        volume = int(max(0, min(volume, 38)))
        self._write_command(zone, "VO", f"{volume:02}")

    @synchronized
    def set_treble(self, zone: int, treble: int) -> None:
        _LOGGER.warning("Treble is not supported on Monoprice 31028 amplifiers")

    @synchronized
    def set_bass(self, zone: int, bass: int) -> None:
        _LOGGER.warning("Bass is not supported on Monoprice 31028 amplifiers")

    @synchronized
    def set_balance(self, zone: int, balance: int) -> None:
        wire_balance = self._balance_internal_to_wire(balance)
        self._write_command(zone, "BA", f"{wire_balance:02}")

    @synchronized
    def set_source(self, zone: int, source: int) -> None:
        source = int(max(1, min(source, 6)))
        self._write_command(zone, "IS", f"{source}")

    @synchronized
    def restore_zone(self, status: ZoneStatus) -> None:
        self.set_power(status.zone, status.power)
        self.set_mute(status.zone, status.mute)
        self.set_volume(status.zone, status.volume)
        self.set_balance(status.zone, status.balance)
        self.set_source(status.zone, status.source)

class MonopriceAsync:
    def __init__(
        self, monoprice_protocol: MonopriceProtocol, lock: asyncio.Lock
    ) -> None:
        """
        Async Monoprice amplifier interface
        """
        self._protocol = monoprice_protocol
        self._lock = lock

    @locked_coro
    async def zone_status(self, zone: int) -> ZoneStatus | None:
        """
        Get the structure representing the status of the zone
        :param zone: zone 11..16, 21..26, 31..36
        :return: status of the zone or None
        """
        # Reading two lines as the response is in the form \r\n#>110001000010111210040\r\n#
        string = await self._protocol.send(_format_zone_status_request(zone), num_eols_to_read=2)
        return ZoneStatus.from_string(string)

    @locked_coro
    async def all_zone_status(self, unit: int) -> list[ZoneStatus]:
        """
        Get the structure representing the status of all zones in a unit
        :param unit: 1, 2, 3
        :return: list of all statuses of the unit's zones or empty list if unit number is incorrect
        """
        if unit < 1 or unit > 3:
            return []
        # Reading 7 lines, since response starts with EOL and each zone's status is followed by EOL
        response = await self._protocol.send(
            _format_all_zones_status_request(unit), num_eols_to_read=7
        )
        return ZoneStatus.from_strings(response.split(sep=EOL.decode('ascii')))

    @locked_coro
    async def set_power(self, zone: int, power: bool) -> None:
        """
        Turn zone on or off
        :param zone: zone 11..16, 21..26, 31..36
        :param power: True to turn on, False to turn off
        """
        await self._protocol.send(_format_set_power(zone, power))

    @locked_coro
    async def set_mute(self, zone: int, mute: bool) -> None:
        """
        Mute zone on or off
        :param zone: zone 11..16, 21..26, 31..36
        :param mute: True to mute, False to unmute
        """
        await self._protocol.send(_format_set_mute(zone, mute))

    @locked_coro
    async def set_volume(self, zone: int, volume: int) -> None:
        """
        Set volume for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param volume: integer from 0 to 38 inclusive
        """
        await self._protocol.send(_format_set_volume(zone, volume))

    @locked_coro
    async def set_treble(self, zone: int, treble: int) -> None:
        """
        Set treble for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param treble: integer from 0 to 14 inclusive, where 0 is -7 treble and 14 is +7
        """
        await self._protocol.send(_format_set_treble(zone, treble))

    @locked_coro
    async def set_bass(self, zone: int, bass: int) -> None:
        """
        Set bass for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param bass: integer from 0 to 14 inclusive, where 0 is -7 bass and 14 is +7
        """
        await self._protocol.send(_format_set_bass(zone, bass))

    @locked_coro
    async def set_balance(self, zone: int, balance: int) -> None:
        """
        Set balance for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param balance: integer from 0 to 20 inclusive, where 0 is -10(left), 0 is center and 20 is +10 (right)
        """
        await self._protocol.send(_format_set_balance(zone, balance))

    @locked_coro
    async def set_source(self, zone: int, source: int) -> None:
        """
        Set source for zone
        :param zone: zone 11..16, 21..26, 31..36
        :param source: integer from 0 to 6 inclusive
        """
        await self._protocol.send(_format_set_source(zone, source))

    @locked_coro
    async def restore_zone(self, status: ZoneStatus) -> None:
        """
        Restores zone to it's previous state
        :param status: zone state to restore
        """
        await self._protocol.send(_format_set_power(status.zone, status.power))
        await self._protocol.send(_format_set_mute(status.zone, status.mute))
        await self._protocol.send(_format_set_volume(status.zone, status.volume))
        await self._protocol.send(_format_set_treble(status.zone, status.treble))
        await self._protocol.send(_format_set_bass(status.zone, status.bass))
        await self._protocol.send(_format_set_balance(status.zone, status.balance))
        await self._protocol.send(_format_set_source(status.zone, status.source))


class MonopriceAsyncProxy:
    def __init__(self, monoprice: Monoprice31028, loop: asyncio.AbstractEventLoop) -> None:
        self._monoprice = monoprice
        self._loop = loop
        self._executor = ThreadPoolExecutor(max_workers=1)

    async def zone_status(self, zone: int) -> ZoneStatus | None:
        return await self._loop.run_in_executor(self._executor, self._monoprice.zone_status, zone)

    async def all_zone_status(self, unit: int) -> list[ZoneStatus]:
        return await self._loop.run_in_executor(self._executor, self._monoprice.all_zone_status, unit)

    async def set_power(self, zone: int, power: bool) -> None:
        await self._loop.run_in_executor(self._executor, self._monoprice.set_power, zone, power)

    async def set_mute(self, zone: int, mute: bool) -> None:
        await self._loop.run_in_executor(self._executor, self._monoprice.set_mute, zone, mute)

    async def set_volume(self, zone: int, volume: int) -> None:
        await self._loop.run_in_executor(self._executor, self._monoprice.set_volume, zone, volume)

    async def set_treble(self, zone: int, treble: int) -> None:
        await self._loop.run_in_executor(self._executor, self._monoprice.set_treble, zone, treble)

    async def set_bass(self, zone: int, bass: int) -> None:
        await self._loop.run_in_executor(self._executor, self._monoprice.set_bass, zone, bass)

    async def set_balance(self, zone: int, balance: int) -> None:
        await self._loop.run_in_executor(self._executor, self._monoprice.set_balance, zone, balance)

    async def set_source(self, zone: int, source: int) -> None:
        await self._loop.run_in_executor(self._executor, self._monoprice.set_source, zone, source)

    async def restore_zone(self, status: ZoneStatus) -> None:
        await self._loop.run_in_executor(self._executor, self._monoprice.restore_zone, status)

class MonopriceProtocol(asyncio.Protocol):
    def __init__(self) -> None:
        super().__init__()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._transport: SerialTransport = None
        self._connected = asyncio.Event()
        self.q: asyncio.Queue[bytes] = asyncio.Queue()

    def connection_made(self, transport: SerialTransport) -> None:
        self._transport = transport
        self._connected.set()
        _LOGGER.debug("port opened %s", self._transport)

    def data_received(self, data: bytes) -> None:
        task = asyncio.create_task(self.q.put(data))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @connected
    @locked_coro
    async def send(self, request: bytes, num_eols_to_read: int = 1) -> str:
        """
        :param request: request that is sent to the monoprice
        :param num_eols_to_read: number of EOL sequences to read. When last EOL is read, reading stops
        :return: ascii string returned by monoprice
        """
        result = bytearray()
        self._transport.serial.reset_output_buffer()
        self._transport.serial.reset_input_buffer()
        while not self.q.empty():
            self.q.get_nowait()
        self._transport.write(request)
        count = None
        try:
            while True:
                result += await asyncio.wait_for(self.q.get(), TIMEOUT)
                count = _subsequence_count(result, EOL, count)
                if count[1] >= num_eols_to_read:
                    break
        except asyncio.TimeoutError:
            _LOGGER.error(
                "Timeout during receiving response for command '%s', received='%s'",
                request,
                result,
            )
            raise
        ret = bytes(result)
        _LOGGER.debug('Received "%s"', ret)
        return ret.decode("ascii")

# Helpers


def _configure_serial_port(port: serial.Serial) -> None:
    port.baudrate = 9600
    port.stopbits = serial.STOPBITS_ONE
    port.bytesize = serial.EIGHTBITS
    port.parity = serial.PARITY_NONE
    port.timeout = TIMEOUT
    port.write_timeout = TIMEOUT


def _subsequence_count(sequence: bytearray, sub: bytes, previous: tuple[int, int] | None = None) -> tuple[int, int]:
    """
    Counts number of subsequences in a sequence
    """
    start, count = (previous or (0, 0))
    while True:
        idx = sequence.find(sub, start)
        if idx < 0:
            return start, count
        start, count = idx + len(sub), count + 1


def _format_zone_status_request(zone: int) -> bytes:
    return "?{}\r".format(zone).encode()


def _format_all_zones_status_request(unit: int) -> bytes:
    return "?{}\r".format(unit * 10).encode()


def _format_set_power(zone: int, power: bool) -> bytes:
    return "<{}PR{}\r".format(zone, "01" if power else "00").encode()


def _format_set_mute(zone: int, mute: bool) -> bytes:
    return "<{}MU{}\r".format(zone, "01" if mute else "00").encode()


def _format_set_volume(zone: int, volume: int) -> bytes:
    volume = int(max(0, min(volume, 38)))
    return "<{}VO{:02}\r".format(zone, volume).encode()


def _format_set_treble(zone: int, treble: int) -> bytes:
    treble = int(max(0, min(treble, 14)))
    return "<{}TR{:02}\r".format(zone, treble).encode()


def _format_set_bass(zone: int, bass: int) -> bytes:
    bass = int(max(0, min(bass, 14)))
    return "<{}BS{:02}\r".format(zone, bass).encode()


def _format_set_balance(zone: int, balance: int) -> bytes:
    balance = max(0, min(balance, 20))
    return "<{}BL{:02}\r".format(zone, balance).encode()


def _format_set_source(zone: int, source: int) -> bytes:
    source = int(max(1, min(source, 6)))
    return "<{}CH{:02}\r".format(zone, source).encode()


def _normalize_model_argument(model: str | None) -> str | None:
    if model is None:
        return None
    normalized = model.strip().lower()
    if not normalized or normalized == "auto":
        return None
    if normalized in (MODEL_10761, MODEL_31028):
        return normalized
    raise ValueError(f"Unsupported Monoprice model '{model}'")


def _read_serial_response(
    port: serial.Serial, request: bytes, terminator: bytes, count: int
) -> bytes:
    port.reset_output_buffer()
    port.reset_input_buffer()
    port.write(request)
    port.flush()
    result = bytearray()
    subseq: tuple[int, int] | None = None
    while True:
        c = port.read(1)
        if not c:
            raise serial.SerialTimeoutException(
                "Connection timed out! Last received bytes {}".format(
                    [hex(a) for a in result]
                )
            )
        result += c
        subseq = _subsequence_count(result, terminator, subseq)
        if subseq[1] >= count:
            break
    return bytes(result)


def _try_detect_10761(port: serial.Serial) -> bool:
    try:
        response = _read_serial_response(
            port, _format_zone_status_request(11), EOL, 2
        )
    except serial.SerialTimeoutException:
        return False
    decoded = response.decode("ascii", errors="ignore")
    return ZONE_PATTERN.search(decoded) is not None


def _try_detect_31028(port: serial.Serial) -> bool:
    try:
        response = _read_serial_response(port, b"?1VO+", PLUS_TERMINATOR, 1)
    except serial.SerialTimeoutException:
        return False
    cleaned = response.decode("ascii", errors="ignore")
    return "?1VO" in cleaned


def _detect_model(port_url: str) -> str:
    port = serial.serial_for_url(port_url, do_not_open=True)
    _configure_serial_port(port)
    port.open()
    try:
        if _try_detect_10761(port):
            return MODEL_10761
        if _try_detect_31028(port):
            return MODEL_31028
    finally:
        port.close()
    raise RuntimeError("Unable to detect Monoprice amplifier model")


def get_monoprice(port_url: str, model: str | None = None):
    """Return synchronous Monoprice interface with optional protocol autodetect."""

    normalized = _normalize_model_argument(model)
    detected = normalized or _detect_model(port_url)
    lock = RLock()
    if detected == MODEL_10761:
        return Monoprice(port_url, lock)
    if detected == MODEL_31028:
        return Monoprice31028(port_url, lock)
    raise ValueError(f"Unsupported Monoprice model '{detected}'")


async def get_async_monoprice(port_url: str, model: str | None = None):
    """Return asynchronous Monoprice interface with optional protocol autodetect."""

    loop = asyncio.get_running_loop()
    normalized = _normalize_model_argument(model)
    detected = (
        normalized
        if normalized
        else await loop.run_in_executor(None, _detect_model, port_url)
    )

    if detected == MODEL_31028:
        monoprice = await loop.run_in_executor(
            None, lambda: Monoprice31028(port_url, RLock())
        )
        return MonopriceAsyncProxy(monoprice, loop)

    lock = asyncio.Lock()
    _, protocol = await create_serial_connection(
        loop, MonopriceProtocol, port_url, baudrate=9600
    )
    return MonopriceAsync(protocol, lock)
