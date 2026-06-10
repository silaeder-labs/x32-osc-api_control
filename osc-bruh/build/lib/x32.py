#!/usr/bin/env python3
"""
X32 OSC API — full protocol from the unofficial X32/M32 OSC Remote Protocol doc.

Usage:
  from x32 import X32

  x = X32("192.168.0.108")
  x.ch(1).fader = 0.75
  x.ch(1).eq(2).f = 2500
  x.ch(1).eq(2).g = -3.0
  x.usb.list_dir()
  x.usb.play("Djerv_Rebel_Heart_from_the_serie")
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Low-level OSC wire format
# ---------------------------------------------------------------------------

def osc_pad(s: str | bytes) -> bytes:
    if isinstance(s, str):
        s = s.encode("utf-8")
    pad = (4 - len(s) % 4) % 4
    return s + b"\x00" * pad


def osc_get(path: str | bytes) -> bytes:
    return osc_pad(path) + osc_pad(",")


def osc_set(path: str | bytes, typetag: str, *values: Any) -> bytes:
    msg = osc_pad(path) + osc_pad("," + typetag)
    for t, v in zip(typetag, values):
        if t == "i":
            msg += struct.pack(">i", int(v))
        elif t == "f":
            msg += struct.pack(">f", float(v))
        elif t == "s":
            msg += osc_pad(str(v))
        elif t == "b":
            blob = v if isinstance(v, bytes) else str(v).encode()
            msg += struct.pack(">i", len(blob))
            msg += osc_pad(blob)
    return msg


def osc_bundle(*messages: bytes) -> bytes:
    return osc_pad("#bundle") + struct.pack(">Q", 0) + b"".join(osc_pad(m) for m in messages)


def parse_osc(data: bytes) -> tuple[str, str, list]:
    """Parse an OSC message → (address, typetag, [values…]).

    Returns (addr, tt, []) if values are truncated — never crashes.
    """
    end = data.find(b"\x00")
    if end < 0:
        return "", "", []
    addr = data[:end].decode("utf-8", errors="replace")
    off = (end + 4) & ~3
    if off >= len(data):
        return addr, "", []
    end2 = data.find(b"\x00", off)
    tt = data[off:end2].decode("utf-8", errors="replace") if end2 > off else ""
    off2 = (end2 + 4) & ~3
    vals: list = []
    # Remove leading comma from typetag if present
    types = tt.lstrip(",")
    for t in types:
        if off2 + 4 > len(data):
            break
        if t == "s":
            se = data.find(b"\x00", off2)
            if se < 0:
                vals.append(data[off2:].decode("utf-8", errors="replace"))
                break
            vals.append(data[off2:se].decode("utf-8", errors="replace"))
            off2 = (se + 4) & ~3
        elif t == "i":
            vals.append(struct.unpack_from(">i", data, off2)[0])
            off2 += 4
        elif t == "f":
            vals.append(struct.unpack_from(">f", data, off2)[0])
            off2 += 4
        elif t == "b":
            if off2 + 4 > len(data):
                break
            blen = struct.unpack_from(">i", data, off2)[0]
            off2 += 4
            endb = min(off2 + blen, len(data))
            vals.append(data[off2:endb])
            off2 = (endb + 3) & ~3
        else:
            off2 += 4
    return addr, tt, vals


# ---------------------------------------------------------------------------
# Float ↔ dB conversion (appendix)
# ---------------------------------------------------------------------------

def _fader_to_db(val: float) -> float:
    """Fader level float [0..1] → dB (approximately)."""
    if val <= 0.0:
        return -90.0
    if val < 0.0625:
        return -90.0 + 30.0 * val / 0.0625
    if val < 0.25:
        return -60.0 + 30.0 * (val - 0.0625) / 0.1875
    if val < 0.5:
        return -30.0 + 20.0 * (val - 0.25) / 0.25
    return -10.0 + 20.0 * (val - 0.5) / 0.5


def _db_to_fader(db: float) -> float:
    """dB → fader level float [0..1]."""
    if db <= -90.0:
        return 0.0
    if db < -60.0:
        return 0.0625 * (db + 90.0) / 30.0
    if db < -30.0:
        return 0.0625 + 0.1875 * (db + 60.0) / 30.0
    if db < -10.0:
        return 0.25 + 0.25 * (db + 30.0) / 20.0
    return 0.5 + 0.5 * (db + 10.0) / 20.0


# ---------------------------------------------------------------------------
# Core Client
# ---------------------------------------------------------------------------

class X32Client:
    """Low-level OSC client for the X32.

    Uses a fresh UDP socket per request for maximum reliability.
    """

    def __init__(self, host: str = "192.168.0.64", port: int = 10023, timeout: float = 1.5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._live_sock: Optional[socket.socket] = None

    def _fresh_sock(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", 0))
        s.settimeout(self.timeout)
        return s

    def _ensure_live_sock(self) -> socket.socket:
        if self._live_sock is None:
            self._live_sock = self._fresh_sock()
        return self._live_sock

    def _request(self, msg: bytes, retries: int = 2) -> Optional[bytes]:
        for _ in range(retries):
            s = self._fresh_sock()
            try:
                s.sendto(msg, (self.host, self.port))
                data, _ = s.recvfrom(65536)
                return data
            except socket.timeout:
                continue
            finally:
                s.close()
        return None

    def send(self, msg: bytes) -> None:
        sock = self._ensure_live_sock()
        sock.sendto(msg, (self.host, self.port))

    def recv(self, timeout: Optional[float] = None) -> bytes:
        sock = self._ensure_live_sock()
        sock.settimeout(self.timeout if timeout is None else timeout)
        data, _ = sock.recvfrom(65536)
        return data

    def get(self, path: str, retries: int = 3) -> Optional[list]:
        data = self._request(osc_get(path), retries=retries)
        if data:
            _, _, vals = parse_osc(data)
            return vals
        return None

    def set(self, path: str, typetag: str, *values: Any) -> Optional[list]:
        msg = osc_set(path, typetag, *values)
        data = self._request(msg, retries=2)
        if data:
            _, _, vals = parse_osc(data)
            return vals
        return None

    def get_str(self, path: str, retries: int = 2) -> Optional[str]:
        vals = self.get(path, retries=retries)
        return str(vals[0]) if vals else None

    def get_int(self, path: str, retries: int = 2) -> Optional[int]:
        vals = self.get(path, retries=retries)
        return int(vals[0]) if vals and vals[0] is not None else None

    def get_float(self, path: str, retries: int = 2) -> Optional[float]:
        vals = self.get(path, retries=retries)
        return float(vals[0]) if vals else None

    def set_str(self, path: str, val: str) -> None:
        self.set(path, "s", val)

    def set_int(self, path: str, val: int) -> None:
        self.set(path, "i", val)

    def set_float(self, path: str, val: float) -> None:
        self.set(path, "f", val)

    def info(self) -> Optional[dict]:
        vals = self.get("/info")
        if vals and len(vals) >= 4:
            return {"version": vals[0], "name": vals[1], "model": vals[2], "fw": vals[3]}
        return None

    def status(self) -> Optional[dict]:
        vals = self.get("/status")
        if vals and len(vals) >= 3:
            return {"state": vals[0], "ip": vals[1], "name": vals[2]}
        return None

    def xremote(self, on: bool = True) -> None:
        """Register/unregister for live updates (~10 s timeout)."""
        self.set_str("/xremote", "on" if on else "off")

    def close(self) -> None:
        if self._live_sock is not None:
            self._live_sock.close()
            self._live_sock = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# USB Subsystem
# ---------------------------------------------------------------------------

class USB:
    """USB recorder / stick management."""

    def __init__(self, client: X32Client):
        self._c = client

    # -- path navigation --

    @property
    def path(self) -> str:
        v = self._c.get_str("/-usb/path")
        return v or ""

    @path.setter
    def path(self, dirname: str) -> None:
        self._c.set_str("/-usb/path", dirname)

    @property
    def title(self) -> str:
        v = self._c.get_str("/-usb/title")
        return v or ""

    @title.setter
    def title(self, filename: str) -> None:
        self._c.set_str("/-usb/title", filename)

    @property
    def dirpos(self) -> int:
        return self._c.get_int("/-usb/dir/dirpos") or 0

    @dirpos.setter
    def dirpos(self, pos: int) -> None:
        self._c.set_int("/-usb/dir/dirpos", pos)

    @property
    def maxpos(self) -> int:
        return self._c.get_int("/-usb/dir/maxpos") or 0

    # -- directory listing --

    @dataclass
    class Entry:
        pos: int
        name: str
        is_dir: bool = False

    def list_dir(self, max_entries: int = 200) -> list[Entry]:
        """List current USB directory, scanning until gap or max."""
        time.sleep(0.3)
        entries: list[USB.Entry] = []
        for i in range(1, max_entries + 1):
            name = self._c.get_str(f"/-usb/dir/{i:03d}/name", retries=1)
            if name is None or name == "":
                if i == 1:
                    time.sleep(0.5)
                    name = self._c.get_str(f"/-usb/dir/{i:03d}/name", retries=2)
                    if name is None or name == "":
                        break
                else:
                    break
            is_dir = name.startswith("[") and name.endswith("]")
            entries.append(USB.Entry(pos=i, name=name, is_dir=is_dir))
        return entries

    def songs(self) -> list[Entry]:
        return [e for e in self.list_dir() if not e.is_dir]

    def dirs(self) -> list[Entry]:
        return [e for e in self.list_dir() if e.is_dir]

    def enter(self, dirname: str) -> None:
        """Navigate into a subdirectory."""
        self.path = dirname
        time.sleep(0.5)

    def go_root(self) -> None:
        """Navigate back to root."""
        self.path = ""
        time.sleep(0.5)

    # -- playback --

    def select(self, pos_or_name: int | str) -> None:
        """Select a track by position number or by name."""
        if isinstance(pos_or_name, int):
            self.dirpos = pos_or_name
        else:
            self.title = pos_or_name
        time.sleep(0.2)

    def play(self, pos_or_name: Optional[int | str] = None) -> None:
        """Select (optional) and start playback via recselect."""
        if pos_or_name is not None:
            if isinstance(pos_or_name, int):
                self._c.set_int("/-action/recselect", pos_or_name)
            else:
                self.title = pos_or_name
                time.sleep(0.3)
                entries = self.list_dir()
                for e in entries:
                    if e.name == pos_or_name:
                        self._c.set_int("/-action/recselect", e.pos)
                        return
                raise ValueError(f"Track '{pos_or_name}' not found")
        else:
            self._c.set_int("/-stat/tape/state", 2)

    def stop(self) -> None:
        self._c.set_int("/-stat/tape/state", 0)

    def pause(self) -> None:
        self._c.set_int("/-stat/tape/state", 1)

    def next_track(self) -> None:
        self._c.set_int("/-action/playtrack", 1)

    def prev_track(self) -> None:
        self._c.set_int("/-action/playtrack", -1)

    @property
    def tape_state(self) -> int:
        return self._c.get_int("/-stat/tape/state") or 0

    @property
    def tape_file(self) -> Optional[str]:
        return self._c.get_str("/-stat/tape/file")

    @property
    def is_mounted(self) -> bool:
        v = self._c.get_int("/-stat/usbmounted")
        return v == 1 if v is not None else False


# ---------------------------------------------------------------------------
# Channel strip abstraction
# ---------------------------------------------------------------------------

class EQBand:
    """One band of a parametric EQ."""

    def __init__(self, client: X32Client, prefix: str, band: int):
        self._c = client
        self._pre = f"{prefix}/eq/{band}"

    @property
    def type(self) -> str:
        return self._c.get_str(f"{self._pre}/type") or ""

    @type.setter
    def type(self, v: str | int) -> None:
        self._c.set(f"{self._pre}/type", "s" if isinstance(v, str) else "i", v)

    @property
    def f(self) -> float:
        return self._c.get_float(f"{self._pre}/f") or 0.0

    @f.setter
    def f(self, hz: float) -> None:
        self._c.set_float(f"{self._pre}/f", hz)

    @property
    def g(self) -> float:
        return self._c.get_float(f"{self._pre}/g") or 0.0

    @g.setter
    def g(self, db: float) -> None:
        self._c.set_float(f"{self._pre}/g", db)

    @property
    def q(self) -> float:
        return self._c.get_float(f"{self._pre}/q") or 0.0

    @q.setter
    def q(self, val: float) -> None:
        self._c.set_float(f"{self._pre}/q", val)

    def set_all(self, typ: str | int, freq: float, gain: float, q: float) -> None:
        self._c.set(f"{self._pre}", "siff" if isinstance(typ, str) else "iff", typ, freq, gain, q)


class EQ:
    """Full EQ (4 or 6 bands)."""

    def __init__(self, client: X32Client, prefix: str, bands: int = 4):
        self._c = client
        self._pre = f"{prefix}/eq"
        self._bands = bands

    def __getitem__(self, band: int) -> EQBand:
        if band < 1 or band > self._bands:
            raise IndexError(f"band {band} out of range [1..{self._bands}]")
        return EQBand(self._c, self._pre, band)

    @property
    def on(self) -> bool:
        v = self._c.get_int(f"{self._pre}/on")
        return v == 1 if v is not None else False

    @on.setter
    def on(self, val: bool) -> None:
        self._c.set_int(f"{self._pre}/on", 1 if val else 0)


class Dynamics:
    """Gate / Compressor / Expander."""

    def __init__(self, client: X32Client, prefix: str, section: str):
        self._c = client
        self._pre = f"{prefix}/{section}"

    @property
    def on(self) -> bool:
        v = self._c.get_int(f"{self._pre}/on")
        return v == 1 if v is not None else False

    @on.setter
    def on(self, val: bool) -> None:
        self._c.set_int(f"{self._pre}/on", 1 if val else 0)

    @property
    def mode(self) -> str:
        return self._c.get_str(f"{self._pre}/mode") or ""

    @mode.setter
    def mode(self, val: str) -> None:
        self._c.set_str(f"{self._pre}/mode", val)

    @property
    def thr(self) -> float:
        return self._c.get_float(f"{self._pre}/thr") or 0.0

    @thr.setter
    def thr(self, db: float) -> None:
        self._c.set_float(f"{self._pre}/thr", db)

    @property
    def ratio(self) -> float:
        return self._c.get_float(f"{self._pre}/ratio") or 1.0

    @ratio.setter
    def ratio(self, val: float) -> None:
        self._c.set_float(f"{self._pre}/ratio", val)

    @property
    def attack(self) -> float:
        return self._c.get_float(f"{self._pre}/attack") or 0.0

    @attack.setter
    def attack(self, ms: float) -> None:
        self._c.set_float(f"{self._pre}/attack", ms)

    @property
    def release(self) -> float:
        return self._c.get_float(f"{self._pre}/release") or 0.0

    @release.setter
    def release(self, ms: float) -> None:
        self._c.set_float(f"{self._pre}/release", ms)

    @property
    def knee(self) -> float:
        return self._c.get_float(f"{self._pre}/knee") or 0.0

    @knee.setter
    def knee(self, val: float) -> None:
        self._c.set_float(f"{self._pre}/knee", val)

    @property
    def mgain(self) -> float:
        """Make-up gain (dB)."""
        return self._c.get_float(f"{self._pre}/mgain") or 0.0

    @mgain.setter
    def mgain(self, db: float) -> None:
        self._c.set_float(f"{self._pre}/mgain", db)


class ChannelStrip:
    """A single channel (input, auxin, fxrtn, bus, matrix, main)."""

    def __init__(self, client: X32Client, prefix: str, num: int):
        self._c = client
        if num == 0:
            self._pre = prefix if prefix.startswith("/") else f"/{prefix}"
        else:
            self._pre = f"{prefix}/{num:02d}" if prefix.startswith("/") else f"/{prefix}/{num:02d}"

    # -- config --
    @property
    def name(self) -> str:
        return self._c.get_str(f"{self._pre}/config/name") or ""

    @name.setter
    def name(self, val: str) -> None:
        self._c.set_str(f"{self._pre}/config/name", val)

    @property
    def icon(self) -> int:
        return self._c.get_int(f"{self._pre}/config/icon") or 0

    @icon.setter
    def icon(self, val: int) -> None:
        self._c.set_int(f"{self._pre}/config/icon", val)

    @property
    def color(self) -> int:
        return self._c.get_int(f"{self._pre}/config/color") or 0

    @color.setter
    def color(self, val: int) -> None:
        self._c.set_int(f"{self._pre}/config/color", val)

    # -- preamp (input channels only) --
    @property
    def trim(self) -> float:
        return self._c.get_float(f"{self._pre}/preamp/trim") or 0.0

    @trim.setter
    def trim(self, db: float) -> None:
        self._c.set_float(f"{self._pre}/preamp/trim", db)

    @property
    def phantom(self) -> bool:
        v = self._c.get_int(f"{self._pre}/preamp/hpon")
        return v == 1 if v is not None else False

    @phantom.setter
    def phantom(self, val: bool) -> None:
        self._c.set_int(f"{self._pre}/preamp/hpon", 1 if val else 0)

    @property
    def invert(self) -> bool:
        v = self._c.get_int(f"{self._pre}/preamp/invert")
        return v == 1 if v is not None else False

    @invert.setter
    def invert(self, val: bool) -> None:
        self._c.set_int(f"{self._pre}/preamp/invert", 1 if val else 0)

    @property
    def hpf(self) -> float:
        return self._c.get_float(f"{self._pre}/preamp/hpf") or 0.0

    @hpf.setter
    def hpf(self, hz: float) -> None:
        self._c.set_float(f"{self._pre}/preamp/hpf", hz)

    # -- mix / fader / mute --
    @property
    def fader(self) -> float:
        return self._c.get_float(f"{self._pre}/mix/fader") or 0.0

    @fader.setter
    def fader(self, val: float) -> None:
        self._c.set_float(f"{self._pre}/mix/fader", val)

    @property
    def fader_db(self) -> float:
        return _fader_to_db(self.fader)

    @fader_db.setter
    def fader_db(self, db: float) -> None:
        self.fader = _db_to_fader(db)

    @property
    def on(self) -> bool:
        v = self._c.get_int(f"{self._pre}/mix/on")
        return v == 1 if v is not None else False

    @on.setter
    def on(self, val: bool) -> None:
        self._c.set_int(f"{self._pre}/mix/on", 1 if val else 0)

    def mute(self) -> None:
        self.on = False

    def unmute(self) -> None:
        self.on = True

    @property
    def pan(self) -> float:
        return self._c.get_float(f"{self._pre}/mix/pan") or 0.0

    @pan.setter
    def pan(self, val: float) -> None:
        self._c.set_float(f"{self._pre}/mix/pan", val)

    @property
    def st(self) -> bool:
        v = self._c.get_int(f"{self._pre}/mix/st")
        return v == 1 if v is not None else False

    @st.setter
    def st(self, val: bool) -> None:
        self._c.set_int(f"{self._pre}/mix/st", 1 if val else 0)

    @property
    def mono(self) -> bool:
        v = self._c.get_int(f"{self._pre}/mix/mono")
        return v == 1 if v is not None else False

    @mono.setter
    def mono(self, val: bool) -> None:
        self._c.set_int(f"{self._pre}/mix/mono", 1 if val else 0)

    @property
    def mlevel(self) -> float:
        return self._c.get_float(f"{self._pre}/mix/mlevel") or 0.0

    @mlevel.setter
    def mlevel(self, val: float) -> None:
        self._c.set_float(f"{self._pre}/mix/mlevel", val)

    # -- sends (bus sends for ch/auxin/fxrtn) --
    def send_on(self, bus: int) -> bool:
        v = self._c.get_int(f"{self._pre}/mix/{bus:02d}/on")
        return v == 1 if v is not None else False

    def send_on_set(self, bus: int, val: bool) -> None:
        self._c.set_int(f"{self._pre}/mix/{bus:02d}/on", 1 if val else 0)

    def send_level(self, bus: int) -> float:
        return self._c.get_float(f"{self._pre}/mix/{bus:02d}/level") or 0.0

    def send_level_set(self, bus: int, val: float) -> None:
        self._c.set_float(f"{self._pre}/mix/{bus:02d}/level", val)

    # -- insert --
    @property
    def insert(self) -> bool:
        v = self._c.get_int(f"{self._pre}/insert/on")
        return v == 1 if v is not None else False

    @insert.setter
    def insert(self, val: bool) -> None:
        self._c.set_int(f"{self._pre}/insert/on", 1 if val else 0)

    @property
    def insert_sel(self) -> int:
        return self._c.get_int(f"{self._pre}/insert/sel") or 0

    @insert_sel.setter
    def insert_sel(self, val: int) -> None:
        self._c.set_int(f"{self._pre}/insert/sel", val)

    # -- gate --
    @property
    def gate(self) -> Dynamics:
        return Dynamics(self._c, self._pre, "gate")

    # -- compressor / dynamics --
    @property
    def dyn(self) -> Dynamics:
        return Dynamics(self._c, self._pre, "dyn")

    # -- EQ --
    @property
    def eq(self) -> EQ:
        bands = 6 if self._pre.startswith(("/bus/", "/mtx/", "/main/")) else 4
        return EQ(self._c, self._pre, bands)

    # -- groups --
    @property
    def dca(self) -> int:
        return self._c.get_int(f"{self._pre}/grp/dca") or 0

    @dca.setter
    def dca(self, mask: int) -> None:
        self._c.set_int(f"{self._pre}/grp/dca", mask)

    @property
    def mute_grp(self) -> int:
        return self._c.get_int(f"{self._pre}/grp/mute") or 0

    @mute_grp.setter
    def mute_grp(self, mask: int) -> None:
        self._c.set_int(f"{self._pre}/grp/mute", mask)

    # -- delay (input ch only) --
    @property
    def delay_on(self) -> bool:
        v = self._c.get_int(f"{self._pre}/delay/on")
        return v == 1 if v is not None else False

    @delay_on.setter
    def delay_on(self, val: bool) -> None:
        self._c.set_int(f"{self._pre}/delay/on", 1 if val else 0)

    @property
    def delay_time(self) -> float:
        return self._c.get_float(f"{self._pre}/delay/time") or 0.0

    @delay_time.setter
    def delay_time(self, ms: float) -> None:
        self._c.set_float(f"{self._pre}/delay/time", ms)


# ---------------------------------------------------------------------------
# Top-level X32 class
# ---------------------------------------------------------------------------

class X32:
    """High-level API for the X32/M32 digital mixer.

    Usage:
        x = X32("192.168.0.108")
        x.ch(1).fader = 0.75
        x.ch(2).eq(3).f = 5000
        x.usb.play(7)
        print(x.info)
    """

    def __init__(self, host: str = "192.168.0.64", port: int = 10023, timeout: float = 3.0):
        self._client = X32Client(host, port, timeout)
        self.usb = USB(self._client)
        self._dca_cache: dict[int, ChannelStrip] = {}

    @property
    def client(self) -> X32Client:
        return self._client

    @property
    def info(self) -> Optional[dict]:
        return self._client.info()

    @property
    def status(self) -> Optional[dict]:
        return self._client.status()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None  # type: ignore

    # -- channel accessors --

    def ch(self, num: int) -> ChannelStrip:
        """Input channel 1-32."""
        if num < 1 or num > 32:
            raise ValueError("ch: 1-32")
        return ChannelStrip(self._client, "/ch", num)

    def auxin(self, num: int) -> ChannelStrip:
        """Aux input 1-8."""
        if num < 1 or num > 8:
            raise ValueError("auxin: 1-8")
        return ChannelStrip(self._client, "/auxin", num)

    def fxrtn(self, num: int) -> ChannelStrip:
        """FX return 1-8."""
        if num < 1 or num > 8:
            raise ValueError("fxrtn: 1-8")
        return ChannelStrip(self._client, "/fxrtn", num)

    def bus(self, num: int) -> ChannelStrip:
        """Bus master 1-16."""
        if num < 1 or num > 16:
            raise ValueError("bus: 1-16")
        return ChannelStrip(self._client, "/bus", num)

    def mtx(self, num: int) -> ChannelStrip:
        """Matrix 1-6."""
        if num < 1 or num > 6:
            raise ValueError("mtx: 1-6")
        return ChannelStrip(self._client, "/mtx", num)

    @property
    def main_st(self) -> ChannelStrip:
        return ChannelStrip(self._client, "/main/st", 0)

    @property
    def main_m(self) -> ChannelStrip:
        return ChannelStrip(self._client, "/main/m", 0)

    def dca(self, num: int) -> ChannelStrip:
        """DCA group 1-8 (limited to fader/on/name)."""
        if num < 1 or num > 8:
            raise ValueError("dca: 1-8")
        if num not in self._dca_cache:
            self._dca_cache[num] = ChannelStrip(self._client, "/dca", num)
        return self._dca_cache[num]

    # -- headamps --

    def headamp(self, idx: int) -> dict:
        """Headamp gain/phantom at index 0-127."""
        if idx < 0 or idx > 127:
            raise ValueError("headamp: 0-127")
        return {
            "gain": self._client.get_float(f"/headamp/{idx:03d}/gain"),
            "phantom": self._client.get_int(f"/headamp/{idx:03d}/phantom") == 1,
        }

    def set_headamp(self, idx: int, gain: Optional[float] = None, phantom: Optional[bool] = None) -> None:
        if idx < 0 or idx > 127:
            raise ValueError("headamp: 0-127")
        if gain is not None:
            self._client.set_float(f"/headamp/{idx:03d}/gain", gain)
        if phantom is not None:
            self._client.set_int(f"/headamp/{idx:03d}/phantom", 1 if phantom else 0)

    # -- FX --

    def fx_type(self, slot: int) -> Optional[str]:
        if slot < 1 or slot > 8:
            raise ValueError("fx slot: 1-8")
        return self._client.get_str(f"/fx/{slot}/type")

    def fx_set_type(self, slot: int, typ: str | int) -> None:
        if slot < 1 or slot > 8:
            raise ValueError("fx slot: 1-8")
        self._client.set(f"/fx/{slot}/type", "s" if isinstance(typ, str) else "i", typ)

    def fx_param(self, slot: int, param: int) -> Optional[float]:
        if slot < 1 or slot > 8 or param < 1 or param > 64:
            raise ValueError("fx slot:1-8 param:1-64")
        return self._client.get_float(f"/fx/{slot}/par/{param:02d}")

    def fx_set_param(self, slot: int, param: int, value: float) -> None:
        if slot < 1 or slot > 8 or param < 1 or param > 64:
            raise ValueError("fx slot:1-8 param:1-64")
        self._client.set_float(f"/fx/{slot}/par/{param:02d}", value)

    # -- status shortcuts --

    @property
    def selidx(self) -> int:
        return self._client.get_int("/-stat/selidx") or 0

    @selidx.setter
    def selidx(self, val: int) -> None:
        self._client.set_int("/-stat/selidx", val)

    @property
    def solo(self) -> bool:
        v = self._client.get_int("/-stat/solo")
        return v == 1 if v is not None else False

    def clear_solo(self) -> None:
        self._client.set_int("/-action/clearsolo", 1)

    @property
    def lock(self) -> bool:
        v = self._client.get_int("/-stat/lock")
        return v == 1 if v is not None else False

    # -- scene/cue/snippet --

    def go_scene(self, num: int) -> None:
        if num < 0 or num > 99:
            raise ValueError("scene: 0-99")
        self._client.set_int("/-action/goscene", num)

    def go_cue(self, num: int) -> None:
        if num < 0 or num > 99:
            raise ValueError("cue: 0-99")
        self._client.set_int("/-action/gocue", num)

    def go_snippet(self, num: int) -> None:
        if num < 0 or num > 99:
            raise ValueError("snippet: 0-99")
        self._client.set_int("/-action/gosnippet", num)

    # -- config shortcuts --

    @property
    def osc_level(self) -> float:
        return self._client.get_float("/config/osc/level") or 0.0

    @osc_level.setter
    def osc_level(self, val: float) -> None:
        self._client.set_float("/config/osc/level", val)

    @property
    def talk_enable(self) -> bool:
        v = self._client.get_int("/config/talk/enable")
        return v == 1 if v is not None else False

    @talk_enable.setter
    def talk_enable(self, val: bool) -> None:
        self._client.set_int("/config/talk/enable", 1 if val else 0)

    # -- meters --

    def meters(self, meter_id: int, *args: int) -> Optional[bytes]:
        tag = f"/meters/{meter_id}"
        if args:
            msg = osc_set(tag, "i" * len(args), *args)
        else:
            msg = osc_get(tag)
        self._client.send(msg)
        try:
            data = self._client.recv(2.0)
            _, _, vals = parse_osc(data)
            return vals[0] if vals and isinstance(vals[0], bytes) else None
        except socket.timeout:
            return None

    # -- subscriptions / xremote --

    def subscribe(self, path: str, time_factor: int = 0) -> None:
        self._client.set("/subscribe", "si", path, time_factor)

    def unsubscribe(self, name: str = "") -> None:
        if name:
            self._client.set_str("/unsubscribe", name)
        else:
            self._client.send(osc_get("/unsubscribe"))

    def renew(self, name: str = "") -> None:
        if name:
            self._client.set_str("/renew", name)
        else:
            self._client.send(osc_get("/renew"))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import sys
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.64"

    with X32(host) as x:
        print(f"X32 @ {host}")
        print(f"  Info: {x.info}")
        print(f"  Status: {x.status}")

        print(f"\nUSB root directory:")
        for e in x.usb.list_dir():
            kind = "DIR " if e.is_dir else "FILE"
            print(f"  {e.pos:3d}  {kind}  {e.name}")

        dirs = x.usb.dirs()
        if dirs:
            print(f"\nDirectories ({len(dirs)}): {', '.join(d.name.strip('[]') for d in dirs)}")


if __name__ == "__main__":
    main()
