#!/usr/bin/env python3
"""Interactive menu for X32 OSC API."""

import sys
import time
from x32 import X32, _db_to_fader

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.108"


def parse_fader_value(raw: str) -> float:
    raw = raw.strip()
    if raw.lower().endswith("db"):
        return _db_to_fader(float(raw[:-2].strip()))
    return float(raw)


def clear():
    print("\033[2J\033[H", end="")


def has_queries(x: X32) -> bool:
    return bool(getattr(x.client, "supports_queries", False))


def ask_bool(prompt: str) -> bool:
    return input(prompt).strip().lower() in {"y", "yes", "1", "on"}


def menu(can_query: bool) -> str:
    clear()
    print(f"  X32 @ {HOST}  —  Interactive Menu\n")
    if can_query:
        print("  ┌──── Console ────┐")
        print("  │ 1  Info / Status │")
        print("  │ 2  USB listing  │")
        print("  │                 │")
        print("  ├── Channel 1 ────┤")
        print("  │ 3  Read channel │")
        print("  │ 4  Set fader    │")
        print("  │ 5  Mute / Unmute│")
        print("  │ 6  EQ bands     │")
        print("  │ 7  Gate / Dyn   │")
        print("  │                 │")
        print("  ├── Buses ────────┤")
        print("  │ 8  Bus fader    │")
        print("  │ 9  Main fader   │")
        print("  │                 │")
        print("  ├── FX ───────────┤")
        print("  │ 10 FX info      │")
        print("  │                 │")
        print("  ├── Scenes ───────┤")
        print("  │ 11 Go to scene  │")
        print("  │ 12 Clear solo   │")
        print("  │                 │")
        print("  ├── Controls ─────┤")
        print("  │ 13 Headamp      │")
        print("  │ 14 DCA groups   │")
        print("  │ 16 xremote on   │")
        print("  │ 17 xremote off  │")
        print("  │                 │")
        print("  └─────────────────┘")
    else:
        print("  MIDI SysEx write-only mode\n")
        print("  ┌──── Control ────┐")
        print("  │ 4  Ch fader     │")
        print("  │ 5  Ch mute      │")
        print("  │ 6  Ch EQ set    │")
        print("  │ 7  Ch gate/dyn  │")
        print("  │ 8  Bus fader    │")
        print("  │ 9  Main fader   │")
        print("  │ 10 FX set       │")
        print("  │ 11 Go to scene  │")
        print("  │ 12 Clear solo   │")
        print("  │ 13 Headamp set  │")
        print("  │ 14 DCA fader    │")
        print("  └─────────────────┘")
    print("  q — Quit\n")
    return input("  Choice: ").strip()


def cmd_info(x: X32):
    info = x.info or {}
    status = x.status or {}
    if not info and not status:
        print("  No response from mixer.")
        print("  Check the IP address, network connection, and UDP port 10023.")
        return
    print(f"  Model:  {info.get('model', '?')}")
    print(f"  FW:     {info.get('fw', '?')}")
    print(f"  State:  {status.get('state', '?')}")
    print(f"  IP:     {status.get('ip', '?')}")
    print(f"  Name:   {status.get('name', '?')}")
    print(f"  Lock:   {x.lock}")
    print(f"  Solo:   {x.solo}")
    print(f"  Selidx: {x.selidx}")


def cmd_usb(x: X32):
    if not x.usb.is_mounted:
        print("  USB not mounted.")
        return

    def show_entries(entries):
        print(f"  Path:  {x.usb.path!r}")
        print(f"  Track: {x.usb.tape_file or '-'}")
        print(f"  Tape state: {x.usb.tape_state}")
        if not entries:
            print("  (empty directory)")
            return
        print("  Entries:")
        for entry in entries:
            kind = "DIR " if entry.is_dir else "FILE"
            print(f"    {entry.pos:3d}  {kind}  {entry.name}")

    while True:
        print()
        entries = x.usb.list_dir()
        show_entries(entries)
        print("\n  Commands: number=open/play, cd <id|dir[/subdir]>, root, up, play <n>, stop, pause, next, prev, upload <filename> [scene_num], q")
        raw = input("  USB> ").strip()
        if not raw or raw.lower() == "q":
            return

        parts = raw.split(maxsplit=1)
        cmd = parts[0].lower()

        if cmd == "root":
            x.usb.go_root()
            continue
        if cmd == "up":
            x.usb.enter("..")
            continue
        if cmd == "cd":
            target = parts[1].strip() if len(parts) > 1 else ""
            if not target:
                print("  Usage: cd <id|dir[/subdir]>")
                continue
            if target == "..":
                x.usb.enter("..")
                continue
            if target.isdigit():
                pos = int(target)
                entry = next((item for item in entries if item.pos == pos), None)
                if entry is None:
                    print(f"  Entry {pos} not found")
                    continue
                if not entry.is_dir:
                    print(f"  Entry {pos} is not a directory")
                    continue
                x.usb.enter(entry.name.strip("[]"))
                continue
            x.usb.cd(target)
            continue
        if cmd == "upload":
            target = parts[1].strip() if len(parts) > 1 else ""
            if not target:
                print("  Usage: upload <filename> [scene_num]")
                continue
            scene_num = None
            uparts = target.split()
            fname = uparts[0]
            if len(uparts) > 1:
                scene_num = int(uparts[1])
            x.usb.upload(fname, scene_num)
            print(f"  Uploaded as: {fname}")
            continue
        if cmd == "stop":
            x.usb.stop()
            continue
        if cmd == "pause":
            x.usb.pause()
            continue
        if cmd == "next":
            x.usb.next_track()
            continue
        if cmd == "prev":
            x.usb.prev_track()
            continue

        target = raw if cmd.isdigit() else parts[1] if len(parts) > 1 else ""
        if cmd == "play" and not target:
            x.usb.play()
            continue

        try:
            pos = int(target)
        except ValueError:
            print(f"  Unknown command: {raw}")
            continue

        entry = next((item for item in entries if item.pos == pos), None)
        if entry is None:
            print(f"  Entry {pos} not found")
            continue
        if entry.is_dir:
            x.usb.enter(entry.name.strip("[]"))
            continue
        x.usb.play(pos)
        print(f"  Playing: {entry.name}")


def cmd_read_ch(x: X32):
    n = int(input("  Channel number (1-32): "))
    c = x.ch(n)
    print(f"  Name:    {c.name!r}")
    print(f"  On:      {c.on}")
    print(f"  Fader:   {c.fader:.4f}  ({c.fader_db:.1f} dB)")
    print(f"  Pan:     {c.pan:.2f}")
    print(f"  Delay:   {c.delay_time:.1f} ms")
    ha = x.headamp(n - 1)
    print(f"  Gain:    {ha['gain']} dB")
    print(f"  Phantom: {ha['phantom']}")


def cmd_set_fader(x: X32):
    n = int(input("  Channel number: "))
    val = parse_fader_value(input("  Fader (0..1 or dB with suffix): "))
    c = x.ch(n)
    c.fader = val
    print(f"  Set ch {n} fader to {val:.4f}")


def cmd_mute(x: X32):
    n = int(input("  Channel number: "))
    c = x.ch(n)
    if has_queries(x):
        if c.on:
            c.mute()
            print(f"  Muted ch {n}")
        else:
            c.unmute()
            print(f"  Unmuted ch {n}")
    else:
        target = ask_bool("  Mute channel? (Y/n): ")
        if target:
            c.mute()
            print(f"  Muted ch {n}")
        else:
            c.unmute()
            print(f"  Unmuted ch {n}")


def cmd_eq(x: X32):
    if not has_queries(x):
        n = int(input("  Channel number: "))
        band_no = int(input("  Band number (1-4): "))
        band = x.ch(n).eq[band_no]
        typ = input("  Type (blank to skip): ").strip()
        if typ:
            band.type = typ
        raw = input("  Frequency Hz (blank to skip): ").strip()
        if raw:
            band.f = float(raw)
        raw = input("  Gain dB (blank to skip): ").strip()
        if raw:
            band.g = float(raw)
        raw = input("  Q (blank to skip): ").strip()
        if raw:
            band.q = float(raw)
        print(f"  Updated EQ band {band_no} on ch {n}")
        return
    n = int(input("  Channel number: "))
    c = x.ch(n)
    print(f"  EQ on: {c.eq.on}")
    for b in range(1, 5):
        band = c.eq[b]
        print(f"  Band {b}: f={band.f:.1f} g={band.g:.1f} q={band.q:.2f}")


def cmd_gate(x: X32):
    if not has_queries(x):
        n = int(input("  Channel number: "))
        c = x.ch(n)
        target = input("  Section (gate/dyn): ").strip().lower()
        sec = c.gate if target != "dyn" else c.dyn
        raw = input("  On? (y/N, blank to skip): ").strip().lower()
        if raw:
            sec.on = raw in {"y", "yes", "1", "on"}
        raw = input("  Threshold dB (blank to skip): ").strip()
        if raw:
            sec.thr = float(raw)
        raw = input("  Attack ms (blank to skip): ").strip()
        if raw:
            sec.attack = float(raw)
        raw = input("  Release ms (blank to skip): ").strip()
        if raw:
            sec.release = float(raw)
        print(f"  Updated {target or 'gate'} on ch {n}")
        return
    n = int(input("  Channel number: "))
    c = x.ch(n)
    g = c.gate
    d = c.dyn
    print("  ── Gate ──")
    print(f"  On:   {g.on}")
    print(f"  Thr:  {g.thr:.2f}")
    print(f"  Atk:  {g.attack:.1f} ms")
    print(f"  Rel:  {g.release:.1f} ms")
    print("  ── Compressor ──")
    print(f"  On:   {d.on}")
    print(f"  Thr:  {d.thr:.2f}")
    print(f"  Atk:  {d.attack:.1f} ms")
    print(f"  Rel:  {d.release:.1f} ms")


def cmd_bus(x: X32):
    if not has_queries(x):
        n = int(input("  Bus number (1-16): "))
        v = parse_fader_value(input("  Fader (0..1 or dB with suffix): "))
        x.bus(n).fader = v
        print(f"  Set bus {n} fader to {v:.4f}")
        return
    n = int(input("  Bus number (1-16): "))
    b = x.bus(n)
    print(f"  Fader: {b.fader:.4f} ({b.fader_db:.1f} dB)")
    print(f"  On:    {b.on}")
    print(f"  Pan:   {b.pan:.2f}")
    r = input("  Set fader? (y/N): ")
    if r.lower() == "y":
        v = float(input("  Value (0..1): "))
        b.fader = v
        print(f"  Set bus {n} fader to {v:.4f}")


def cmd_main(x: X32):
    if not has_queries(x):
        v = parse_fader_value(input("  Fader (0..1 or dB with suffix): "))
        x.main_st.fader = v
        print(f"  Set main fader to {v:.4f}")
        return
    m = x.main_st
    print(f"  Fader:   {m.fader:.4f} ({m.fader_db:.1f} dB)")
    print(f"  On:      {m.on}")
    r = input("  Set fader? (y/N): ")
    if r.lower() == "y":
        v = float(input("  Value (0..1): "))
        m.fader = v
        print(f"  Set main fader to {v:.4f}")


def cmd_fx(x: X32):
    if not has_queries(x):
        slot = int(input("  Slot (1-8): "))
        kind = input("  Change type or param? (type/param): ").strip().lower()
        if kind == "type":
            typ = input("  FX type: ").strip()
            x.fx_set_type(slot, typ)
            print(f"  Set FX slot {slot} type to {typ}")
        else:
            param = int(input("  Param number (1-64): "))
            value = float(input("  Param value: "))
            x.fx_set_param(slot, param, value)
            print(f"  Set FX slot {slot} param {param} to {value}")
        return
    for s in range(1, 9):
        t = x.fx_type(s)
        print(f"  Slot {s}: type={t}")
    slot = int(input("  Slot to inspect (1-8): "))
    for p in [1, 3, 5, 10, 20, 30, 40, 50, 60]:
        v = x.fx_param(slot, p)
        if v is not None:
            print(f"    param {p:02d} = {v:.3f}")


def cmd_scene(x: X32):
    n = int(input("  Scene number (0-99): "))
    x.go_scene(n)
    print(f"  Go scene {n}")


def cmd_clear_solo(x: X32):
    x.clear_solo()
    print("  Solo cleared")


def cmd_headamp(x: X32):
    if not has_queries(x):
        idx = int(input("  Headamp index (0-127): "))
        raw = input("  Gain (-6..60 dB, blank to skip): ").strip()
        if raw:
            x.set_headamp(idx, gain=float(raw))
            print(f"  Set gain to {raw}")
        raw = input("  Phantom on/off (on/off, blank to skip): ").strip().lower()
        if raw:
            x.set_headamp(idx, phantom=raw in {"on", "1", "y", "yes"})
            print(f"  Phantom set to {raw}")
        return
    idx = int(input("  Headamp index (0-127): "))
    ha = x.headamp(idx)
    print(f"  Gain:    {ha['gain']}")
    print(f"  Phantom: {ha['phantom']}")
    r = input("  Set gain? (y/N): ")
    if r.lower() == "y":
        g = float(input("  Gain (-6..60 dB): "))
        x.set_headamp(idx, gain=g)
        print(f"  Set gain to {g}")
    r = input("  Toggle phantom? (y/N): ")
    if r.lower() == "y":
        x.set_headamp(idx, phantom=not ha["phantom"])
        print(f"  Toggled phantom")


def cmd_dca(x: X32):
    if not has_queries(x):
        n = int(input("  DCA number (1-8): "))
        v = parse_fader_value(input("  Fader (0..1 or dB with suffix): "))
        x.dca(n).fader = v
        print(f"  Set DCA {n} fader to {v:.4f}")
        return
    n = int(input("  DCA number (1-8): "))
    d = x.dca(n)
    print(f"  Fader: {d.fader:.4f} ({d.fader_db:.1f} dB)")
    print(f"  On:    {d.on}")
    r = input("  Set fader? (y/N): ")
    if r.lower() == "y":
        v = float(input("  Value (0..1): "))
        d.fader = v
        print(f"  Set DCA {n} fader to {v:.4f}")


def cmd_xremote(x: X32, on: bool):
    x.xremote(on)
    print(f"  xremote {'on' if on else 'off'}")


def main():
    with X32(HOST) as x:
        can_query = has_queries(x)
        while True:
            ch = menu(can_query)
            if ch == "q":
                break

            print()
            t0 = time.time()
            try:
                match ch:
                    case "1":
                        cmd_info(x)
                    case "2":
                        cmd_usb(x)
                    case "3":
                        cmd_read_ch(x)
                    case "4":
                        cmd_set_fader(x)
                    case "5":
                        cmd_mute(x)
                    case "6":
                        cmd_eq(x)
                    case "7":
                        cmd_gate(x)
                    case "8":
                        cmd_bus(x)
                    case "9":
                        cmd_main(x)
                    case "10":
                        cmd_fx(x)
                    case "11":
                        cmd_scene(x)
                    case "12":
                        cmd_clear_solo(x)
                    case "13":
                        cmd_headamp(x)
                    case "14":
                        cmd_dca(x)
                    case "16":
                        cmd_xremote(x, True)
                    case "17":
                        cmd_xremote(x, False)
                    case _:
                        print(f"  Unknown: {ch}")
            except (ValueError, IndexError) as e:
                print(f"  Error: {e}")
            except Exception as e:
                print(f"  Error: {type(e).__name__}: {e}")

            elapsed = time.time() - t0
            print(f"\n  ({elapsed:.1f}s)")
            input("  Press Enter...")


if __name__ == "__main__":
    main()
