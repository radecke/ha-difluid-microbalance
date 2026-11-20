"""Pure frame building and parsing for the DiFluid Microbalance (no HA imports).

Frame layout: ``DF DF <func> <cmd> <len> <data...> <checksum>``
checksum = sum of all preceding bytes (header included) & 0xFF.

Reference: https://github.com/DiFluid/difluid-sdk-demo/blob/master/docs/protocolMicrobalance.md
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

HEADER = b"\xdf\xdf"
MIN_FRAME_LEN = 6  # header(2) + func + cmd + len + checksum

GRAMS_PER_OUNCE = 28.349523125

# Func / Cmd identifiers
FUNC_INFO = 0x00
FUNC_SETTINGS = 0x01
FUNC_DEVICE = 0x03

CMD_INFO_SERIAL = 0x00  # answered in 3 packets: data0 = packet no, data1-5 ASCII
CMD_INFO_MODEL = 0x01  # answered with an ASCII string
CMD_INFO_FIRMWARE = 0x02  # answered with an ASCII string

SERIAL_PACKETS = 3

# Func 1 settings: get = len 0, set = len 1; the response echoes the value.
CMD_SETTINGS_AUTO_SEND = 0x00
CMD_SETTINGS_AUTO_DETECT_TIMING = 0x01
CMD_SETTINGS_AUTO_STOP_TIMING = 0x02
CMD_SETTINGS_UNIT_SWITCH_LOCK = 0x03
CMD_SETTINGS_WEIGHT_UNIT = 0x04

CMD_DEVICE_SENSOR = 0x00
CMD_DEVICE_DLINK_PRESS = 0x01
CMD_DEVICE_SINGLE_CLICK = 0x02
CMD_DEVICE_DOUBLE_CLICK = 0x03
CMD_DEVICE_LONG_PRESS = 0x04
CMD_DEVICE_STATUS = 0x05

# Button single-click data values (Func 3 Cmd 2)
CLICK_DLINK = 0x00  # DLink single click -> timer start (per doc: "used for timer")
CLICK_POWER = 0x01  # Power single click -> tare

# Key response frames (Func 3 Cmd 2/3/4): 0/1 = echo of an app command
# (DLink/Power), 2/3 = triggered on the device (DLink/Power).
# Func 3 Cmd 1 (DLink press): 0 = app echo, 1 = triggered on the device.
KEY_DEVICE_DLINK = 0x02
KEY_DEVICE_POWER = 0x03
PRESS_DEVICE = 0x01

# Event types for physical key presses (DLink key = timer key).
KEY_POWER_CLICK = "power_click"
KEY_TIMER_CLICK = "timer_click"
KEY_POWER_DOUBLE = "power_double"
KEY_TIMER_DOUBLE = "timer_double"
KEY_POWER_LONG = "power_long"
KEY_TIMER_LONG = "timer_long"
KEY_TIMER_PRESS = "timer_press"
KEY_EVENT_TYPES: tuple[str, ...] = (
    KEY_POWER_CLICK,
    KEY_TIMER_CLICK,
    KEY_POWER_DOUBLE,
    KEY_TIMER_DOUBLE,
    KEY_POWER_LONG,
    KEY_TIMER_LONG,
    KEY_TIMER_PRESS,
)
_KEY_SUFFIX = {
    CMD_DEVICE_SINGLE_CLICK: "click",
    CMD_DEVICE_DOUBLE_CLICK: "double",
    CMD_DEVICE_LONG_PRESS: "long",
}


class WeightUnit(IntEnum):
    """Unit the scale is currently displaying (sensor frame data12)."""

    GRAM = 0
    OUNCE = 1
    GERING = 2


class DeviceState(IntEnum):
    """Device status enum (status frame data0), per DiFluid doc."""

    POWER_DOWN = 0
    CHARGING = 1
    LOW_POWER_MODE_1 = 2
    LOW_BATTERY_SHUTDOWN = 3
    STARTUP = 4
    IDLE = 5
    SHOW_DEVICE_INFO = 6
    TARE_IN_PROGRESS = 7
    OTA_IN_PROGRESS = 8
    OTA_FAILED = 9
    TIMING = 10
    TIMER_PAUSE = 11
    RESERVED = 12
    LOW_POWER_MODE_2 = 13
    AUTO_STOP_TIMING_TRIGGER = 14
    # Not in the DiFluid doc. Observed 2026-11-20: one DLink click from IDLE
    # leads here, the next click starts TIMING.
    TIMER_READY = 15


@dataclass(frozen=True)
class Frame:
    """A raw, checksum-verified frame."""

    func: int
    cmd: int
    data: bytes


@dataclass(frozen=True)
class SensorData:
    """Decoded sensor frame (Func 3 Cmd 0)."""

    weight_raw: int  # signed int32, scaled by unit (x10 g / x1000 oz / x10 gering)
    flow_raw: int  # signed int16, x10
    timer_raw: int  # unsigned int16, unit undocumented
    tick: int  # device timestamp, not exposed
    unit: int  # WeightUnit value (kept int to tolerate unknown values)

    @property
    def weight(self) -> float:
        """Weight in the scale's own display unit."""
        if self.unit == WeightUnit.OUNCE:
            return self.weight_raw / 1000
        return self.weight_raw / 10

    @property
    def weight_grams(self) -> float | None:
        """Weight converted to grams; None if the unit cannot be converted.

        "gering" has no documented conversion to grams, so it yields None.
        """
        if self.unit == WeightUnit.GRAM:
            return self.weight_raw / 10
        if self.unit == WeightUnit.OUNCE:
            return round(self.weight_raw / 1000 * GRAMS_PER_OUNCE, 2)
        return None

    @property
    def flow_rate(self) -> float:
        """Flow rate (doc: x10; assumed g/s)."""
        return self.flow_raw / 10

    @property
    def timer(self) -> int:
        """Raw timer value — unit not documented by DiFluid."""
        return self.timer_raw


@dataclass(frozen=True)
class StatusData:
    """Decoded device status frame (Func 3 Cmd 5)."""

    state: int  # DeviceState value
    battery: int  # 0..100 %
    charging: bool
    raw: bytes

    @property
    def state_name(self) -> str:
        """Lower-case name of the device state, or ``unknown_<n>``."""
        try:
            return DeviceState(self.state).name.lower()
        except ValueError:
            return f"unknown_{self.state}"


def checksum(data: bytes) -> int:
    """Return the frame checksum over ``data``."""
    return sum(data) & 0xFF


def build_frame(func: int, cmd: int, data: bytes = b"") -> bytes:
    """Build a complete frame including checksum."""
    body = HEADER + bytes((func, cmd, len(data))) + data
    return body + bytes((checksum(body),))


def build_get_setting(cmd: int) -> bytes:
    """Build a Func 1 get request (len 0) for setting ``cmd``."""
    return build_frame(FUNC_SETTINGS, cmd)


def build_set_setting(cmd: int, value: int) -> bytes:
    """Build a Func 1 set request (len 1) for setting ``cmd``."""
    return build_frame(FUNC_SETTINGS, cmd, bytes((value & 0xFF,)))


# Ready-made commands
CMD_GET_STATUS = build_frame(FUNC_DEVICE, CMD_DEVICE_STATUS)
CMD_GET_SENSOR = build_frame(FUNC_DEVICE, CMD_DEVICE_SENSOR)
CMD_AUTO_SEND_ON = build_frame(FUNC_SETTINGS, CMD_SETTINGS_AUTO_SEND, b"\x01")
CMD_AUTO_SEND_OFF = build_frame(FUNC_SETTINGS, CMD_SETTINGS_AUTO_SEND, b"\x00")
CMD_TARE = build_frame(FUNC_DEVICE, CMD_DEVICE_SINGLE_CLICK, bytes((CLICK_POWER,)))
CMD_TIMER_START = build_frame(
    FUNC_DEVICE, CMD_DEVICE_SINGLE_CLICK, bytes((CLICK_DLINK,))
)
CMD_TIMER_STOP = build_frame(FUNC_DEVICE, CMD_DEVICE_DLINK_PRESS, b"\x00")
# Func 3 Cmd 4 data 1: Power long press = shutdown per DiFluid doc. Tested
# 2026-11-20: the scale acknowledges (cmd 4 data 03, 01) but stays on. Unused.
CMD_POWER_OFF = build_frame(FUNC_DEVICE, CMD_DEVICE_LONG_PRESS, bytes((CLICK_POWER,)))
CMD_GET_SERIAL = build_frame(FUNC_INFO, CMD_INFO_SERIAL)
CMD_GET_MODEL = build_frame(FUNC_INFO, CMD_INFO_MODEL)
CMD_GET_FIRMWARE = build_frame(FUNC_INFO, CMD_INFO_FIRMWARE)


class FrameBuffer:
    """Accumulates notification bytes and yields complete, valid frames.

    Notifications may contain several concatenated frames or a partial one;
    junk before a header and frames with a bad checksum are skipped.
    """

    MAX_BUFFER = 1024

    def __init__(self) -> None:
        """Initialize an empty buffer."""
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[Frame]:
        """Append ``chunk`` and return all frames that are now complete."""
        self._buf.extend(chunk)
        frames: list[Frame] = []
        while True:
            start = self._buf.find(HEADER)
            if start < 0:
                # Keep a trailing 0xDF: it may be the first half of a header.
                keep = 1 if self._buf[-1:] == b"\xdf" else 0
                del self._buf[: len(self._buf) - keep]
                break
            if start:
                del self._buf[:start]
            if len(self._buf) < MIN_FRAME_LEN:
                break
            total = MIN_FRAME_LEN + self._buf[4]
            if len(self._buf) < total:
                break
            candidate = bytes(self._buf[:total])
            if checksum(candidate[:-1]) != candidate[-1]:
                # Bad frame: drop one byte and resync on the next header.
                del self._buf[:1]
                continue
            del self._buf[:total]
            frames.append(Frame(candidate[2], candidate[3], candidate[5:-1]))
        if len(self._buf) > self.MAX_BUFFER:
            self._buf.clear()
        return frames

    def clear(self) -> None:
        """Drop buffered bytes (e.g. after a reconnect)."""
        self._buf.clear()


def parse_sensor(frame: Frame) -> SensorData | None:
    """Decode a Func 3 Cmd 0 frame; None if it is not one."""
    if frame.func != FUNC_DEVICE or frame.cmd != CMD_DEVICE_SENSOR:
        return None
    d = frame.data
    if len(d) < 13:
        return None
    return SensorData(
        weight_raw=int.from_bytes(d[0:4], "big", signed=True),
        flow_raw=int.from_bytes(d[4:6], "big", signed=True),
        timer_raw=int.from_bytes(d[6:8], "big", signed=False),
        tick=int.from_bytes(d[8:12], "big", signed=False),
        unit=d[12],
    )


def parse_status(frame: Frame) -> StatusData | None:
    """Decode a Func 3 Cmd 5 frame; None if it is not one."""
    if frame.func != FUNC_DEVICE or frame.cmd != CMD_DEVICE_STATUS:
        return None
    d = frame.data
    if len(d) < 3:
        return None
    return StatusData(state=d[0], battery=d[1], charging=d[2] == 1, raw=bytes(d))


def parse_setting(frame: Frame) -> tuple[int, int] | None:
    """Decode a Func 1 response into ``(cmd, value)``; None if not one."""
    if frame.func != FUNC_SETTINGS or len(frame.data) < 1:
        return None
    return frame.cmd, frame.data[0]


def is_key_frame(frame: Frame) -> bool:
    """Return True for Func 3 key response frames (Cmd 1..4)."""
    return frame.func == FUNC_DEVICE and frame.cmd in (
        CMD_DEVICE_DLINK_PRESS,
        CMD_DEVICE_SINGLE_CLICK,
        CMD_DEVICE_DOUBLE_CLICK,
        CMD_DEVICE_LONG_PRESS,
    )


def parse_key_event(frame: Frame) -> str | None:
    """Return the event type of a key pressed on the device itself.

    App command echoes (and anything else) yield None.
    """
    if not is_key_frame(frame) or len(frame.data) < 1:
        return None
    value = frame.data[0]
    if frame.cmd == CMD_DEVICE_DLINK_PRESS:
        return KEY_TIMER_PRESS if value == PRESS_DEVICE else None
    suffix = _KEY_SUFFIX[frame.cmd]
    if value == KEY_DEVICE_DLINK:
        return f"timer_{suffix}"
    if value == KEY_DEVICE_POWER:
        return f"power_{suffix}"
    return None


def decode_ascii(data: bytes) -> str:
    """Decode an ASCII payload, dropping NUL and other non-printable bytes."""
    return "".join(chr(b) for b in data if 0x20 <= b < 0x7F).strip()


def parse_info_string(frame: Frame, cmd: int) -> str | None:
    """Decode a Func 0 model/firmware response; None if not one or empty."""
    if frame.func != FUNC_INFO or frame.cmd != cmd:
        return None
    return decode_ascii(frame.data) or None


class SerialAssembler:
    """Collects the 3 serial-number packets (Func 0 Cmd 0)."""

    def __init__(self) -> None:
        """Initialize empty."""
        self._parts: dict[int, str] = {}

    def reset(self) -> None:
        """Forget collected packets."""
        self._parts.clear()

    def feed(self, frame: Frame) -> str | None:
        """Add a packet; return the full serial once all packets are in."""
        if frame.func != FUNC_INFO or frame.cmd != CMD_INFO_SERIAL:
            return None
        if len(frame.data) < 2 or frame.data[0] >= SERIAL_PACKETS:
            return None
        self._parts[frame.data[0]] = decode_ascii(frame.data[1:6])
        if len(self._parts) < SERIAL_PACKETS:
            return None
        serial = "".join(self._parts[i] for i in range(SERIAL_PACKETS))
        self._parts.clear()
        return serial or None


def _hex(s: str) -> bytes:
    return bytes.fromhex(s)


if __name__ == "__main__":
    # Commands match the captured / documented byte sequences.
    assert _hex("df df 03 05 00 c6") == CMD_GET_STATUS
    assert _hex("df df 03 00 00 c1") == CMD_GET_SENSOR
    assert _hex("df df 01 00 01 01 c1") == CMD_AUTO_SEND_ON
    assert _hex("df df 03 02 01 01 c5") == CMD_TARE
    assert _hex("df df 03 02 01 00 c4") == CMD_TIMER_START
    assert _hex("df df 03 01 01 00 c3") == CMD_TIMER_STOP

    # 1: single sensor frame
    buf = FrameBuffer()
    frames = buf.feed(_hex("df df 03 00 0d ff ff fc ac 00 00 00 00 00 00 e9 c0 00 1d"))
    assert len(frames) == 1, frames
    s = parse_sensor(frames[0])
    assert s is not None
    assert s.weight == -85.2 and s.weight_grams == -85.2, s
    assert s.flow_rate == 0 and s.timer == 0
    assert s.unit == WeightUnit.GRAM

    # 2: echo + sensor frame in one notification
    buf = FrameBuffer()
    frames = buf.feed(
        _hex(
            "df df 01 00 01 01 c1 "
            "df df 03 00 0d ff ff fc ac 00 00 00 00 00 00 eb f0 00 4f"
        )
    )
    assert len(frames) == 2, frames
    assert (frames[0].func, frames[0].cmd, frames[0].data) == (1, 0, b"\x01")
    s = parse_sensor(frames[1])
    assert s is not None and s.weight_grams == -85.2

    # 3: status frame
    buf = FrameBuffer()
    frames = buf.feed(_hex("df df 03 05 08 05 1e 00 00 00 00 00 00 f1"))
    assert len(frames) == 1
    st = parse_status(frames[0])
    assert st is not None
    assert st.state == DeviceState.IDLE and st.state_name == "idle"
    assert st.battery == 30 and st.charging is False

    # Doc example status: idle, 100 %, charging
    st = parse_status(buf.feed(_hex("df df 03 05 08 05 64 01 00 00 00 00 00 38"))[0])
    assert st is not None and st.battery == 100 and st.charging

    # Robustness: split across notifications, leading junk, bad checksum
    buf = FrameBuffer()
    data = _hex("00 11 df df 03 00 0d ff ff fc ac 00 00 00 00 00 00 e9 c0 00 1d")
    assert buf.feed(data[:5]) == []
    assert buf.feed(data[5:12]) == []
    assert len(buf.feed(data[12:])) == 1
    assert buf.feed(_hex("df df 03 05 00 c7")) == []  # bad checksum dropped
    assert len(buf.feed(CMD_GET_STATUS)) == 1  # resyncs afterwards

    # Device info: serial number in 3 packets (doc example)
    assert CMD_GET_SERIAL == _hex("df df 00 00 00 be")
    buf = FrameBuffer()
    sn_frames = buf.feed(
        _hex(
            "df df 00 00 06 00 36 38 42 36 42 ec "
            "df df 00 00 06 01 33 32 34 31 37 c6 "
            "df df 00 00 06 02 42 30 30 30 30 c8"
        )
    )
    assert len(sn_frames) == 3, sn_frames
    asm = SerialAssembler()
    assert asm.feed(sn_frames[0]) is None
    assert asm.feed(sn_frames[1]) is None
    assert asm.feed(sn_frames[2]) == "68B6B32417B0000"
    # Out-of-order packets still assemble
    assert asm.feed(sn_frames[2]) is None and asm.feed(sn_frames[0]) is None
    assert asm.feed(sn_frames[1]) == "68B6B32417B0000"

    # Model / firmware: ASCII, parsed by the actual frame, NUL bytes stripped
    f = buf.feed(build_frame(FUNC_INFO, CMD_INFO_MODEL, b"DFT-S101"))[0]
    assert parse_info_string(f, CMD_INFO_MODEL) == "DFT-S101"
    assert parse_info_string(f, CMD_INFO_FIRMWARE) is None
    f = buf.feed(build_frame(FUNC_INFO, CMD_INFO_FIRMWARE, b"V001\x00\x00"))[0]
    assert parse_info_string(f, CMD_INFO_FIRMWARE) == "V001"

    # Settings responses (doc examples)
    f = buf.feed(_hex("df df 01 04 01 01 c5"))
    assert len(f) == 1 and parse_setting(f[0]) == (CMD_SETTINGS_WEIGHT_UNIT, 1)
    assert WeightUnit(parse_setting(f[0])[1]) == WeightUnit.OUNCE
    f = buf.feed(_hex("df df 01 03 01 00 c3"))
    assert len(f) == 1
    assert parse_setting(f[0]) == (CMD_SETTINGS_UNIT_SWITCH_LOCK, 0)
    assert build_get_setting(CMD_SETTINGS_WEIGHT_UNIT) == _hex("df df 01 04 00 c3")
    assert build_set_setting(CMD_SETTINGS_WEIGHT_UNIT, 1) == _hex("df df 01 04 01 01 c5")
    assert build_set_setting(CMD_SETTINGS_UNIT_SWITCH_LOCK, 0) == _hex(
        "df df 01 03 01 00 c3"
    )

    # Key frames: device-triggered vs. app echoes
    f = buf.feed(_hex("df df 03 02 01 03 c7"))
    assert len(f) == 1 and parse_key_event(f[0]) == KEY_POWER_CLICK
    f = buf.feed(_hex("df df 03 02 01 02 c6"))
    assert len(f) == 1 and parse_key_event(f[0]) == KEY_TIMER_CLICK
    for cmd, dlink, power in (
        (CMD_DEVICE_DOUBLE_CLICK, KEY_TIMER_DOUBLE, KEY_POWER_DOUBLE),
        (CMD_DEVICE_LONG_PRESS, KEY_TIMER_LONG, KEY_POWER_LONG),
    ):
        assert parse_key_event(Frame(FUNC_DEVICE, cmd, b"\x02")) == dlink
        assert parse_key_event(Frame(FUNC_DEVICE, cmd, b"\x03")) == power
        assert parse_key_event(Frame(FUNC_DEVICE, cmd, b"\x00")) is None
        assert parse_key_event(Frame(FUNC_DEVICE, cmd, b"\x01")) is None
    assert parse_key_event(Frame(FUNC_DEVICE, CMD_DEVICE_DLINK_PRESS, b"\x01")) == (
        KEY_TIMER_PRESS
    )
    # Echoes of our own commands (tare, timer start/stop) are no events
    for cmd_frame in (CMD_TARE, CMD_TIMER_START, CMD_TIMER_STOP, CMD_POWER_OFF):
        echo = buf.feed(cmd_frame)[0]
        assert is_key_frame(echo) and parse_key_event(echo) is None
    assert set(KEY_EVENT_TYPES) == {
        parse_key_event(Frame(FUNC_DEVICE, c, bytes((v,))))
        for c in (2, 3, 4)
        for v in (2, 3)
    } | {KEY_TIMER_PRESS}

    print("protocol self-test OK")
