#!/usr/bin/env python3
"""Enumerate X32/M32 USB files via OSC — with directory navigation."""

import socket, struct, sys, time

OSC_PORT = 10023

def osc_pad(s):
    s = s.encode("utf-8")
    pad = (4 - len(s) % 4) % 4
    return s + b"\x00" * pad

def osc_get(path):
    return osc_pad(path) + osc_pad(",")

def osc_set_str(path, val):
    return osc_pad(path) + osc_pad(",s") + osc_pad(val)

def query(path, host, retries=2, timeout=2):
    for _ in range(retries):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        sock.settimeout(timeout)
        sock.sendto(osc_get(path), (host, OSC_PORT))
        try:
            data, _ = sock.recvfrom(4096)
            end = data.find(b"\x00")
            off = (end + 4) & ~3
            end2 = data.find(b"\x00", off)
            off2 = (end2 + 4) & ~3
            se = data.find(b"\x00", off2)
            val = data[off2:se].decode("utf-8", errors="replace")
            sock.close()
            return val
        except socket.timeout:
            sock.close()
            continue
    return None

def set_path(host, dirname):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(2)
    sock.sendto(osc_set_str("/-usb/path", dirname), (host, OSC_PORT))
    try:
        sock.recvfrom(4096)
    except socket.timeout:
        pass
    sock.close()

def list_dir(host, label="Root"):
    entries = []
    for i in range(1, 200):
        name = query(f"/-usb/dir/{i:03d}/name", host)
        if name is None or name == "":
            break
        is_dir = name.startswith("[") and name.endswith("]")
        entries.append((i, is_dir, name))
    return entries

def navigate(host, target_dir):
    set_path(host, target_dir)
    time.sleep(0.5)
    path = query("/-usb/path", host, retries=3)
    return path

def show(host):
    path = query("/-usb/path", host, retries=3)
    title = query("/-usb/title", host)
    print(f"  Path:  {path or '(empty / root)'}")
    print(f"  Title: {title or '(none)'}")
    entries = list_dir(host)
    if not entries:
        print("  (empty directory)")
        return [], path

    files = []
    print(f"  {'Pos':<5} {'Type':<5} Name")
    print(f"  {'-'*60}")
    for i, is_dir, name in entries:
        kind = "DIR " if is_dir else "FILE"
        print(f"  {i:<5} {kind:<5} {name}")
        if not is_dir:
            files.append(name)
    print(f"  {'-'*60}")
    print(f"  Total: {len(entries)} entries ({len(files)} files)")
    return entries, path

def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.64"

    print(f"X32/M32 USB Browser — {host}:{OSC_PORT}")
    print()

    # Show current directory
    entries, current_path = show(host)

    dirs = [(i, n.strip("[]")) for i, is_dir, n in entries if is_dir and n != "[..]"]

    if not dirs:
        return

    print()
    print("Subdirectories available:")
    for i, name in dirs:
        print(f"  {i}: {name}")

    print()
    target = input("Enter directory number to navigate into (or just Enter to quit): ").strip()
    if not target:
        return

    try:
        idx = int(target)
        dirnames = [n for _, n in dirs]
        if 1 <= idx <= len(dirnames):
            chosen = dirnames[idx - 1]
        else:
            chosen = dirs[idx - 1][1] if 1 <= idx - 1 < len(dirs) else None
    except (ValueError, IndexError):
        chosen = target

    if not chosen:
        print("Invalid choice.")
        return

    print(f"\nNavigating into '{chosen}'...\n")
    new_path = navigate(host, chosen)
    if new_path:
        _, _ = show(host)
    else:
        set_path(host, "")
        time.sleep(0.5)
        print("Failed to navigate. Reset to root.")
        show(host)

if __name__ == "__main__":
    main()
