#!/usr/bin/env python3
"""REST API for X32 OSC control.

Usage:
  python x32_rest_api.py --mixer 192.168.0.64
  python x32_rest_api.py --mixer 192.168.0.64 --port 8080 --timeout 2.0
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from x32 import X32, _db_to_fader, _fader_to_db, parse_osc, osc_get

logger = logging.getLogger("x32-api")

executor = ThreadPoolExecutor(max_workers=4)


async def _run(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(executor, lambda: fn(*args, **kwargs)),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Mixer query timed out")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except (socket.timeout, OSError) as e:
        raise HTTPException(504, f"Mixer unreachable: {e}")
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChannelPatch(BaseModel):
    name: Optional[str] = None
    icon: Optional[int] = None
    color: Optional[int] = None
    fader: Optional[float] = None
    fader_db: Optional[float] = None
    on: Optional[bool] = None
    pan: Optional[float] = None
    st: Optional[bool] = None
    mono: Optional[bool] = None
    trim: Optional[float] = None
    phantom: Optional[bool] = None
    invert: Optional[bool] = None
    hpf: Optional[float] = None
    delay_on: Optional[bool] = None
    delay_time: Optional[float] = None
    dca: Optional[int] = None
    mute_grp: Optional[int] = None
    insert: Optional[bool] = None
    insert_sel: Optional[int] = None


class EQBandPatch(BaseModel):
    type: Optional[str] = None
    f: Optional[float] = None
    g: Optional[float] = None
    q: Optional[float] = None


class DynamicsPatch(BaseModel):
    on: Optional[bool] = None
    mode: Optional[str] = None
    thr: Optional[float] = None
    ratio: Optional[float] = None
    attack: Optional[float] = None
    release: Optional[float] = None
    knee: Optional[float] = None
    mgain: Optional[float] = None


class HeadampPatch(BaseModel):
    gain: Optional[float] = None
    phantom: Optional[bool] = None


class FaderBody(BaseModel):
    fader: Optional[float] = None
    fader_db: Optional[float] = None


class PlayBody(BaseModel):
    pos: Optional[int] = None
    name: Optional[str] = None


class UploadBody(BaseModel):
    filename: str
    scene_num: Optional[int] = None


class FXTypeBody(BaseModel):
    type: str


class FXParamBody(BaseModel):
    value: float


# ---------------------------------------------------------------------------
# Fader Broadcaster — WebSocket push via xremote
# ---------------------------------------------------------------------------

class FaderBroadcaster:
    """Listens to X32 xremote push and broadcasts fader changes to WS clients."""

    def __init__(self, x32: X32):
        self.x32 = x32
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: list[asyncio.Queue] = []
        self._faders: dict[str, dict] = {}
        self._lock = threading.Lock()

    @property
    def faders(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._faders)

    def start(self, loop: asyncio.AbstractEventLoop):
        if self._running:
            return
        self._running = True
        self._loop = loop
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        try:
            self.x32.client.send(osc_get("/xremote"))
            while self._running:
                try:
                    data = self.x32.client.recv(timeout=1.0)
                    addr, tt, vals = parse_osc(data)
                    if not vals:
                        continue
                    info = self._parse_fader(addr, tt, vals)
                    if info:
                        with self._lock:
                            self._faders[info["path"]] = info
                        if self._loop and not self._loop.is_closed():
                            asyncio.run_coro_threadsafe(
                                self._broadcast(info), self._loop
                            )
                except socket.timeout:
                    continue
        except Exception:
            logger.exception("FaderBroadcaster error")

    def _parse_fader(self, addr: str, tt: str, vals: list) -> dict | None:
        parts = addr.strip("/").split("/")
        if len(parts) >= 3:
            kind = parts[0]
            num_s = parts[1]
            param = parts[-1]
            num: int | str = int(num_s) if num_s.isdigit() else num_s
            if param == "fader" and vals:
                return {"path": addr, "type": kind, "num": num, "fader": float(vals[0]), "fader_db": round(_fader_to_db(float(vals[0])), 1)}
            if param == "on" and vals:
                return {"path": addr, "type": kind, "num": num, "on": bool(vals[0])}
            if param == "name" and vals:
                return {"path": addr, "type": kind, "num": num, "name": str(vals[0])}
        return None

    async def _broadcast(self, msg: dict):
        dead: list[asyncio.Queue] = []
        for q in self._queues:
            try:
                await asyncio.wait_for(q.put(msg), timeout=1)
            except Exception:
                dead.append(q)
        for q in dead:
            self._queues.remove(q)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        with self._lock:
            if q in self._queues:
                self._queues.remove(q)


# ---------------------------------------------------------------------------
# Tape Broadcaster — WebSocket push of playback status
# ---------------------------------------------------------------------------

class TapeBroadcaster:
    """Polls X32 tape status and pushes updates to WS clients."""

    def __init__(self, x32: X32):
        self.x32 = x32
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: list[asyncio.Queue] = []
        self._lock = threading.Lock()

    def start(self, loop: asyncio.AbstractEventLoop):
        if self._running:
            return
        self._running = True
        self._loop = loop
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        while self._running:
            try:
                usb = self.x32.usb
                mounted = usb.is_mounted
                if mounted:
                    info = {
                        "mounted": True,
                        "tape_state": usb.tape_state,
                        "tape_file": usb.tape_file,
                        "tape_time": usb.tape_time,
                        "tape_length": usb.tape_length,
                    }
                else:
                    info = {"mounted": False}
                if self._loop and not self._loop.is_closed():
                    asyncio.run_coro_threadsafe(
                        self._broadcast(info), self._loop
                    )
            except Exception:
                logger.exception("TapeBroadcaster error")
            time.sleep(1.0)

    async def _broadcast(self, msg: dict):
        dead: list[asyncio.Queue] = []
        for q in self._queues:
            try:
                await asyncio.wait_for(q.put(msg), timeout=1)
            except Exception:
                dead.append(q)
        for q in dead:
            self._queues.remove(q)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        with self._lock:
            if q in self._queues:
                self._queues.remove(q)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(mixer_host: str, mixer_port: int, timeout: float) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        x32 = await _run(X32, mixer_host, mixer_port, timeout)
        app.state.x32 = x32
        bc = FaderBroadcaster(x32)
        bc.start(asyncio.get_event_loop())
        app.state.fader_bc = bc
        tc = TapeBroadcaster(x32)
        tc.start(asyncio.get_event_loop())
        app.state.tape_bc = tc
        info = await _run(lambda: x32.info) if x32.client.supports_queries else None
        logger.info(f"Connected to mixer @ {mixer_host}:{mixer_port} — {info}")
        yield
        bc.stop()
        tc.stop()
        await _run(x32.close)

    app = FastAPI(title="X32 REST API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    _register_routes(app)
    return app


def _x32(request: Request) -> X32:
    return request.app.state.x32


async def _supports_queries(request: Request) -> bool:
    x32 = _x32(request)
    return bool(await _run(lambda: x32.client.supports_queries))


def _check_queries(supports: bool):
    if not supports:
        raise HTTPException(400, "Mixer does not support queries (MIDI SysEx mode)")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _register_routes(app: FastAPI):
    # -- Mixer info / status --
    @app.get("/api/info")
    async def get_info(request: Request):
        x32 = _x32(request)
        if not await _supports_queries(request):
            return {"version": None, "name": None, "model": None, "fw": None}
        return await _run(lambda: x32.info) or {}

    @app.get("/api/status")
    async def get_status(request: Request):
        x32 = _x32(request)
        if not await _supports_queries(request):
            return {"state": None, "ip": None, "name": None}
        return await _run(lambda: x32.status) or {}

    @app.get("/api/solo")
    async def get_solo(request: Request):
        x32 = _x32(request)
        if not await _supports_queries(request):
            return {"solo": None}
        return {"solo": await _run(lambda: x32.solo)}

    @app.post("/api/solo/clear")
    async def clear_solo(request: Request):
        x32 = _x32(request)
        await _run(x32.clear_solo)
        return {"ok": True}

    @app.get("/api/selidx")
    async def get_selidx(request: Request):
        x32 = _x32(request)
        return {"selidx": await _run(lambda: x32.selidx)}

    @app.put("/api/selidx")
    async def set_selidx(request: Request, body: dict):
        x32 = _x32(request)
        await _run(lambda: setattr(x32, "selidx", body["selidx"]))
        return {"ok": True}

    # -- Channels --
    def _read_channel(x32: X32, num: int) -> dict[str, Any]:
        c = x32.ch(num)
        sq = x32.client.supports_queries
        if not sq:
            return {"num": num}
        return {
            "num": num,
            "name": c.name,
            "icon": c.icon,
            "color": c.color,
            "fader": c.fader,
            "fader_db": c.fader_db,
            "on": c.on,
            "pan": c.pan,
            "st": c.st,
            "mono": c.mono,
            "trim": c.trim,
            "phantom": c.phantom,
            "invert": c.invert,
            "hpf": c.hpf,
            "delay_on": c.delay_on,
            "delay_time": c.delay_time,
            "dca": c.dca,
            "mute_grp": c.mute_grp,
            "insert": c.insert,
            "insert_sel": c.insert_sel,
        }

    def _patch_channel(x32: X32, num: int, body: ChannelPatch) -> None:
        c = x32.ch(num)
        for field, value in body.model_dump(exclude_none=True).items():
            if field == "fader_db":
                c.fader = _db_to_fader(value)
            elif field == "fader":
                c.fader = value
            elif field == "on":
                c.on = value
            elif field == "pan":
                c.pan = value
            elif field == "st":
                c.st = value
            elif field == "mono":
                c.mono = value
            elif field == "name":
                c.name = value
            elif field == "icon":
                c.icon = value
            elif field == "color":
                c.color = value
            elif field == "trim":
                c.trim = value
            elif field == "phantom":
                c.phantom = value
            elif field == "invert":
                c.invert = value
            elif field == "hpf":
                c.hpf = value
            elif field == "delay_on":
                c.delay_on = value
            elif field == "delay_time":
                c.delay_time = value
            elif field == "dca":
                c.dca = value
            elif field == "mute_grp":
                c.mute_grp = value
            elif field == "insert":
                c.insert = value
            elif field == "insert_sel":
                c.insert_sel = value

    @app.get("/api/channels/{num}")
    async def get_channel(request: Request, num: int):
        if num < 1 or num > 32:
            raise HTTPException(400, "Channel number 1-32")
        x32 = _x32(request)
        return await _run(_read_channel, x32, num)

    @app.patch("/api/channels/{num}")
    async def patch_channel(request: Request, num: int, body: ChannelPatch):
        if num < 1 or num > 32:
            raise HTTPException(400, "Channel number 1-32")
        x32 = _x32(request)
        await _run(_patch_channel, x32, num, body)
        return await _run(_read_channel, x32, num)

    @app.get("/api/channels/{num}/eq/{band}")
    async def get_eq_band(request: Request, num: int, band: int):
        if num < 1 or num > 32:
            raise HTTPException(400, "Channel number 1-32")
        if band < 1 or band > 4:
            raise HTTPException(400, "Band 1-4")
        x32 = _x32(request)
        return await _run(lambda: _read_eq(x32, num, band))

    @app.patch("/api/channels/{num}/eq/{band}")
    async def patch_eq_band(request: Request, num: int, band: int, body: EQBandPatch):
        if num < 1 or num > 32:
            raise HTTPException(400, "Channel number 1-32")
        if band < 1 or band > 4:
            raise HTTPException(400, "Band 1-4")
        x32 = _x32(request)
        await _run(_patch_eq, x32, num, band, body)
        return await _run(_read_eq, x32, num, band)

    @app.get("/api/channels/{num}/gate")
    async def get_gate(request: Request, num: int):
        if num < 1 or num > 32:
            raise HTTPException(400, "Channel number 1-32")
        x32 = _x32(request)
        return await _run(_read_dynamics, x32, num, "gate")

    @app.patch("/api/channels/{num}/gate")
    async def patch_gate(request: Request, num: int, body: DynamicsPatch):
        if num < 1 or num > 32:
            raise HTTPException(400, "Channel number 1-32")
        x32 = _x32(request)
        await _run(_patch_dynamics, x32, num, "gate", body)
        return await _run(_read_dynamics, x32, num, "gate")

    @app.get("/api/channels/{num}/dyn")
    async def get_dyn(request: Request, num: int):
        if num < 1 or num > 32:
            raise HTTPException(400, "Channel number 1-32")
        x32 = _x32(request)
        return await _run(_read_dynamics, x32, num, "dyn")

    @app.patch("/api/channels/{num}/dyn")
    async def patch_dyn(request: Request, num: int, body: DynamicsPatch):
        if num < 1 or num > 32:
            raise HTTPException(400, "Channel number 1-32")
        x32 = _x32(request)
        await _run(_patch_dynamics, x32, num, "dyn", body)
        return await _run(_read_dynamics, x32, num, "dyn")

    # -- Aux Inputs --
    @app.get("/api/auxins/{num}")
    async def get_auxin(request: Request, num: int):
        if num < 1 or num > 8:
            raise HTTPException(400, "Aux in 1-8")
        x32 = _x32(request)
        return await _run(_read_channel_auxin, x32, num)

    @app.patch("/api/auxins/{num}")
    async def patch_auxin(request: Request, num: int, body: ChannelPatch):
        if num < 1 or num > 8:
            raise HTTPException(400, "Aux in 1-8")
        x32 = _x32(request)
        await _run(_patch_channel_auxin, x32, num, body)
        return await _run(_read_channel_auxin, x32, num)

    # ... and so on for buses, matrix, main, dca, fx, headamp, usb, scenes

    # For now, let me keep it simpler — I'll register the remaining routes inline

    _register_bus_routes(app)
    _register_main_routes(app)
    _register_dca_routes(app)
    _register_fx_routes(app)
    _register_headamp_routes(app)
    _register_scene_routes(app)
    _register_usb_routes(app)
    _register_misc_routes(app)


# -- Helpers for reading/patching different channel types --

def _read_eq(x32: X32, num: int, band: int) -> dict:
    b = x32.ch(num).eq[band]
    return {
        "type": b.type,
        "f": b.f,
        "g": b.g,
        "q": b.q,
    }


def _patch_eq(x32: X32, num: int, band: int, body: EQBandPatch) -> None:
    b = x32.ch(num).eq[band]
    dump = body.model_dump(exclude_none=True)
    if "type" in dump:
        b.type = dump["type"]
    if "f" in dump:
        b.f = dump["f"]
    if "g" in dump:
        b.g = dump["g"]
    if "q" in dump:
        b.q = dump["q"]


def _read_dynamics(x32: X32, num: int, section: str) -> dict:
    d = getattr(x32.ch(num), "gate" if section == "gate" else "dyn")
    return {
        "on": d.on,
        "mode": d.mode,
        "thr": d.thr,
        "ratio": d.ratio,
        "attack": d.attack,
        "release": d.release,
        "knee": d.knee,
        "mgain": d.mgain,
    }


def _patch_dynamics(x32: X32, num: int, section: str, body: DynamicsPatch) -> None:
    d = getattr(x32.ch(num), "gate" if section == "gate" else "dyn")
    dump = body.model_dump(exclude_none=True)
    for field, value in dump.items():
        setattr(d, field, value)


def _read_channel_auxin(x32: X32, num: int) -> dict:
    c = x32.auxin(num)
    if not x32.client.supports_queries:
        return {"num": num}
    return {
        "num": num,
        "name": c.name,
        "fader": c.fader,
        "fader_db": c.fader_db,
        "on": c.on,
        "pan": c.pan,
    }


def _patch_channel_auxin(x32: X32, num: int, body: ChannelPatch) -> None:
    c = x32.auxin(num)
    dump = body.model_dump(exclude_none=True)
    _apply_channel_patch(c, dump)


def _apply_channel_patch(c, dump: dict) -> None:
    for field, value in dump.items():
        if field == "fader_db":
            c.fader = _db_to_fader(value)
        elif field == "fader":
            c.fader = value
        elif field == "on":
            c.on = value
        elif field == "pan":
            c.pan = value
        elif field == "name":
            c.name = value
        elif field == "st":
            c.st = value
        elif field == "mono":
            c.mono = value


# -- Bus routes --

def _register_bus_routes(app: FastAPI):
    def _read_bus(x32: X32, num: int) -> dict:
        c = x32.bus(num)
        if not x32.client.supports_queries:
            return {"num": num}
        return {
            "num": num,
            "name": c.name,
            "fader": c.fader,
            "fader_db": c.fader_db,
            "on": c.on,
            "pan": c.pan,
            "mono": c.mono,
        }

    def _patch_bus(x32: X32, num: int, body: FaderBody) -> None:
        c = x32.bus(num)
        dump = body.model_dump(exclude_none=True)
        if "fader" in dump:
            c.fader = dump["fader"]
        if "fader_db" in dump:
            c.fader = _db_to_fader(dump["fader_db"])

    @app.get("/api/buses/{num}")
    async def get_bus(request: Request, num: int):
        if num < 1 or num > 16:
            raise HTTPException(400, "Bus number 1-16")
        x32 = _x32(request)
        return await _run(_read_bus, x32, num)

    @app.patch("/api/buses/{num}")
    async def patch_bus(request: Request, num: int, body: FaderBody):
        if num < 1 or num > 16:
            raise HTTPException(400, "Bus number 1-16")
        x32 = _x32(request)
        await _run(_patch_bus, x32, num, body)
        return await _run(_read_bus, x32, num)


# -- Main routes --

def _register_main_routes(app: FastAPI):
    def _read_main(x32: X32, which: str = "st") -> dict:
        c = x32.main_st if which == "st" else x32.main_m
        if not x32.client.supports_queries:
            return {}
        return {
            "fader": c.fader,
            "fader_db": c.fader_db,
            "on": c.on,
            "pan": c.pan if which == "m" else None,
            "mono": c.mono,
        }

    def _patch_main(x32: X32, which: str, body: FaderBody) -> None:
        c = x32.main_st if which == "st" else x32.main_m
        dump = body.model_dump(exclude_none=True)
        if "fader" in dump:
            c.fader = dump["fader"]
        if "fader_db" in dump:
            c.fader = _db_to_fader(dump["fader_db"])

    @app.get("/api/main/st")
    async def get_main_st(request: Request):
        x32 = _x32(request)
        return await _run(_read_main, x32, "st")

    @app.patch("/api/main/st")
    async def patch_main_st(request: Request, body: FaderBody):
        x32 = _x32(request)
        await _run(_patch_main, x32, "st", body)
        return await _run(_read_main, x32, "st")

    @app.get("/api/main/m")
    async def get_main_m(request: Request):
        x32 = _x32(request)
        return await _run(_read_main, x32, "m")

    @app.patch("/api/main/m")
    async def patch_main_m(request: Request, body: FaderBody):
        x32 = _x32(request)
        await _run(_patch_main, x32, "m", body)
        return await _run(_read_main, x32, "m")


# -- DCA routes --

def _register_dca_routes(app: FastAPI):
    def _read_dca(x32: X32, num: int) -> dict:
        if not x32.client.supports_queries:
            return {"num": num}
        c = x32.dca(num)
        return {
            "num": num,
            "name": c.name,
            "fader": c.fader,
            "fader_db": c.fader_db,
            "on": c.on,
        }

    def _patch_dca(x32: X32, num: int, body: FaderBody) -> None:
        c = x32.dca(num)
        dump = body.model_dump(exclude_none=True)
        if "fader" in dump:
            c.fader = dump["fader"]
        if "fader_db" in dump:
            c.fader = _db_to_fader(dump["fader_db"])

    @app.get("/api/dcas/{num}")
    async def get_dca(request: Request, num: int):
        if num < 1 or num > 8:
            raise HTTPException(400, "DCA number 1-8")
        x32 = _x32(request)
        return await _run(_read_dca, x32, num)

    @app.patch("/api/dcas/{num}")
    async def patch_dca(request: Request, num: int, body: FaderBody):
        if num < 1 or num > 8:
            raise HTTPException(400, "DCA number 1-8")
        x32 = _x32(request)
        await _run(_patch_dca, x32, num, body)
        return await _run(_read_dca, x32, num)


# -- FX routes --

def _register_fx_routes(app: FastAPI):
    @app.get("/api/fx/{slot}")
    async def get_fx_type(request: Request, slot: int):
        if slot < 1 or slot > 8:
            raise HTTPException(400, "FX slot 1-8")
        x32 = _x32(request)
        typ = await _run(lambda: x32.fx_type(slot))
        return {"slot": slot, "type": typ}

    @app.put("/api/fx/{slot}/type")
    async def set_fx_type(request: Request, slot: int, body: FXTypeBody):
        if slot < 1 or slot > 8:
            raise HTTPException(400, "FX slot 1-8")
        x32 = _x32(request)
        await _run(x32.fx_set_type, slot, body.type)
        return {"slot": slot, "type": body.type}

    @app.get("/api/fx/{slot}/params/{param}")
    async def get_fx_param(request: Request, slot: int, param: int):
        if slot < 1 or slot > 8 or param < 1 or param > 64:
            raise HTTPException(400, "FX slot 1-8, param 1-64")
        x32 = _x32(request)
        val = await _run(lambda: x32.fx_param(slot, param))
        return {"slot": slot, "param": param, "value": val}

    @app.put("/api/fx/{slot}/params/{param}")
    async def set_fx_param(request: Request, slot: int, param: int, body: FXParamBody):
        if slot < 1 or slot > 8 or param < 1 or param > 64:
            raise HTTPException(400, "FX slot 1-8, param 1-64")
        x32 = _x32(request)
        await _run(x32.fx_set_param, slot, param, body.value)
        return {"slot": slot, "param": param, "value": body.value}


# -- Headamp routes --

def _register_headamp_routes(app: FastAPI):
    @app.get("/api/headamps/{idx}")
    async def get_headamp(request: Request, idx: int):
        if idx < 0 or idx > 127:
            raise HTTPException(400, "Headamp index 0-127")
        x32 = _x32(request)
        if not await _supports_queries(request):
            return {"idx": idx, "gain": None, "phantom": None}
        return await _run(lambda: x32.headamp(idx))

    @app.patch("/api/headamps/{idx}")
    async def patch_headamp(request: Request, idx: int, body: HeadampPatch):
        if idx < 0 or idx > 127:
            raise HTTPException(400, "Headamp index 0-127")
        x32 = _x32(request)
        await _run(x32.set_headamp, idx, body.gain, body.phantom)
        if x32.client.supports_queries:
            return await _run(lambda: x32.headamp(idx))
        return {"ok": True}


# -- Scene / Cue / Snippet routes --

def _register_scene_routes(app: FastAPI):
    @app.post("/api/scene/{num}")
    async def go_scene(request: Request, num: int):
        if num < 0 or num > 99:
            raise HTTPException(400, "Scene number 0-99")
        x32 = _x32(request)
        await _run(x32.go_scene, num)
        return {"ok": True, "scene": num}

    @app.post("/api/cue/{num}")
    async def go_cue(request: Request, num: int):
        if num < 0 or num > 99:
            raise HTTPException(400, "Cue number 0-99")
        x32 = _x32(request)
        await _run(x32.go_cue, num)
        return {"ok": True, "cue": num}

    @app.post("/api/snippet/{num}")
    async def go_snippet(request: Request, num: int):
        if num < 0 or num > 99:
            raise HTTPException(400, "Snippet number 0-99")
        x32 = _x32(request)
        await _run(x32.go_snippet, num)
        return {"ok": True, "snippet": num}


# -- USB routes --

def _register_usb_routes(app: FastAPI):
    @app.get("/api/usb")
    async def usb_status(request: Request):
        x32 = _x32(request)
        usb = x32.usb
        mounted = await _run(lambda: usb.is_mounted)
        return {
            "mounted": mounted,
            "path": await _run(lambda: usb.path) if mounted else None,
            "tape_file": await _run(lambda: usb.tape_file) if mounted else None,
            "tape_state": await _run(lambda: usb.tape_state) if mounted else None,
            "tape_time": await _run(lambda: usb.tape_time) if mounted else None,
            "tape_length": await _run(lambda: usb.tape_length) if mounted else None,
        }

    @app.get("/api/usb/list")
    async def usb_list(request: Request):
        x32 = _x32(request)
        entries = await _run(lambda: x32.usb.list_dir())
        return {
            "entries": [
                {"pos": e.pos, "name": e.name, "is_dir": e.is_dir}
                for e in entries
            ]
        }

    @app.post("/api/usb/play")
    async def usb_play(request: Request, body: PlayBody):
        x32 = _x32(request)
        if body.pos is not None:
            await _run(x32.usb.play, body.pos)
        elif body.name is not None:
            await _run(x32.usb.play, body.name)
        else:
            await _run(x32.usb.play)
        return {"ok": True}

    @app.post("/api/usb/stop")
    async def usb_stop(request: Request):
        x32 = _x32(request)
        await _run(x32.usb.stop)
        return {"ok": True}

    @app.post("/api/usb/pause")
    async def usb_pause(request: Request):
        x32 = _x32(request)
        await _run(x32.usb.pause)
        return {"ok": True}

    @app.post("/api/usb/next")
    async def usb_next(request: Request):
        x32 = _x32(request)
        await _run(x32.usb.next_track)
        return {"ok": True}

    @app.post("/api/usb/prev")
    async def usb_prev(request: Request):
        x32 = _x32(request)
        await _run(x32.usb.prev_track)
        return {"ok": True}

    @app.post("/api/usb/cd")
    async def usb_cd(request: Request, body: dict):
        x32 = _x32(request)
        path = body.get("path", "")
        await _run(x32.usb.cd, path)
        return {"ok": True}

    @app.post("/api/usb/upload")
    async def usb_upload(request: Request, body: UploadBody):
        x32 = _x32(request)
        await _run(x32.usb.upload, body.filename, body.scene_num)
        return {"ok": True, "filename": body.filename}

    @app.websocket("/ws/tape")
    async def ws_tape(websocket: WebSocket):
        await websocket.accept()
        tc: TapeBroadcaster = websocket.app.state.tape_bc
        queue = tc.subscribe()
        try:
            while True:
                msg = await asyncio.wait_for(queue.get(), timeout=30)
                await websocket.send_json({"type": "tape_update", **msg})
        except (asyncio.TimeoutError, WebSocketDisconnect):
            pass
        finally:
            tc.unsubscribe(queue)


# -- Misc routes --

def _register_misc_routes(app: FastAPI):
    @app.get("/api/xremote")
    async def get_xremote(request: Request):
        x32 = _x32(request)
        return {"xremote": bool(await _run(lambda: x32.client.supports_queries))}

    @app.post("/api/xremote")
    async def set_xremote(request: Request, body: dict):
        x32 = _x32(request)
        await _run(x32.xremote, body.get("on", True))
        return {"ok": True}

    @app.websocket("/ws/faders")
    async def ws_faders(websocket: WebSocket):
        await websocket.accept()
        bc: FaderBroadcaster = websocket.app.state.fader_bc
        queue = bc.subscribe()
        try:
            snapshot = bc.faders
            if snapshot:
                await websocket.send_json({"type": "snapshot", "faders": list(snapshot.values())})
            while True:
                msg = await asyncio.wait_for(queue.get(), timeout=30)
                await websocket.send_json({"type": "update", **msg})
        except (asyncio.TimeoutError, WebSocketDisconnect):
            pass
        finally:
            bc.unsubscribe(queue)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def index():
        return {"status": "X32 REST API", "docs": "/docs"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="X32 REST API Server")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP listen address")
    parser.add_argument("--port", type=int, default=8080, help="HTTP listen port")
    parser.add_argument("--mixer", default="192.168.1.108", help="X32 mixer IP")
    parser.add_argument("--mixer-port", type=int, default=10023, help="X32 OSC port")
    parser.add_argument("--timeout", type=float, default=3.0, help="OSC timeout (s)")
    parser.add_argument("--log-level", default="INFO", help="Log level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = create_app(args.mixer, args.mixer_port, args.timeout)
    logger.info(f"Starting server on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
