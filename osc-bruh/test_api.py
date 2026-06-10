#!/usr/bin/env python3
"""Test the X32 REST API and generate swagger.yaml."""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error

try:
    import yaml
except ImportError:
    yaml = None

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"

pass_count = 0
fail_count = 0


def request(method: str, path: str, body=None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body
    except Exception as e:
        return 0, str(e)


def approx(a, b, eps=0.01):
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) < eps
    return a == b


def check(name: str, got, expected, status=200):
    global pass_count, fail_count
    ok = True
    if isinstance(expected, dict):
        for k, v in expected.items():
            if isinstance(got, dict):
                ok = approx(got.get(k), v)
                if not ok:
                    break
            else:
                ok = False
                break
    elif isinstance(expected, (list, tuple)):
        ok = got in expected
    else:
        ok = approx(got, expected)
    if ok:
        pass_count += 1
        print(f"  PASS  {name}")
    else:
        fail_count += 1
        print(f"  FAIL  {name}: expected {expected!r}, got {got!r}")


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Generate swagger.yaml
# ---------------------------------------------------------------------------
section("Generating swagger.yaml")
status, spec = request("GET", "/openapi.json")
if status == 200 and spec:
    with open("/home/H7DRA/Downloads/osc-bruh/swagger.yaml", "w") as f:
        if yaml:
            yaml.dump(spec, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        else:
            json.dump(spec, f, indent=2)
    print(f"  Saved swagger.yaml ({len(json.dumps(spec))} bytes)")
else:
    print(f"  FAILED to fetch OpenAPI spec: {status}")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
section("Health")
status, data = request("GET", "/health")
check("GET /health returns 200", status, 200)
check("status is ok", data, {"status": "ok"})


# ---------------------------------------------------------------------------
# Mixer info / status
# ---------------------------------------------------------------------------
section("Mixer Info & Status")
status, data = request("GET", "/api/info")
check("GET /api/info returns 200", status, 200)
for key in ("model", "fw", "version", "name"):
    check(f"info.{key} exists", key in data, True)

status, data = request("GET", "/api/status")
check("GET /api/status returns 200", status, 200)
for key in ("state", "ip", "name"):
    check(f"status.{key} exists", key in data, True)


# ---------------------------------------------------------------------------
# Solo
# ---------------------------------------------------------------------------
section("Solo")
status, data = request("GET", "/api/solo")
check("GET /api/solo returns 200", status, 200)
check("solo has solo key", "solo" in data, True)

status, data = request("POST", "/api/solo/clear")
check("POST /api/solo/clear returns 200", status, 200)
check("clear solo ok", data, {"ok": True})


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------
section("Channels")

# Invalid channel
status, data = request("GET", "/api/channels/0")
check("GET /api/channels/0 = 400", status, 400)

status, data = request("GET", "/api/channels/33")
check("GET /api/channels/33 = 400", status, 400)

# Valid channel
status, data = request("GET", "/api/channels/1")
check("GET /api/channels/1 returns 200", status, 200)
for key in ("num", "name", "fader", "on"):
    check(f"channel.1 has {key}", key in data, True)

# Read fader
fader_before = data.get("fader", 0)

# Set fader to a test value
test_val = 0.5
status, data = request("PATCH", "/api/channels/1", {"fader": test_val})
check("PATCH /api/channels/1 fader returns 200", status, 200)
check("channel fader was set", data.get("fader"), test_val)

# Restore original
if fader_before != 0.5:
    request("PATCH", "/api/channels/1", {"fader": fader_before})

# Mute toggle
status, data = request("GET", "/api/channels/2")
check("GET /api/channels/2 OK", status, 200)
state_before = data.get("on", True)
new_state = not state_before
status, data = request("PATCH", "/api/channels/2", {"on": new_state})
check("PATCH /api/channels/2 on", data.get("on"), new_state)
request("PATCH", "/api/channels/2", {"on": state_before})


# ---------------------------------------------------------------------------
# Channel EQ
# ---------------------------------------------------------------------------
section("Channel EQ")
status, data = request("GET", "/api/channels/1/eq/1")
check("GET /api/channels/1/eq/1 returns 200", status, 200)
for key in ("type", "f", "g", "q"):
    check(f"eq has {key}", key in data, True)


# ---------------------------------------------------------------------------
# Channel Dynamics
# ---------------------------------------------------------------------------
section("Channel Dynamics")
status, data = request("GET", "/api/channels/1/gate")
check("GET /api/channels/1/gate returns 200", status, 200)
for key in ("on", "thr", "attack", "release"):
    check(f"gate has {key}", key in data, True)

status, data = request("GET", "/api/channels/1/dyn")
check("GET /api/channels/1/dyn returns 200", status, 200)
for key in ("on", "thr", "ratio"):
    check(f"dyn has {key}", key in data, True)


# ---------------------------------------------------------------------------
# Buses
# ---------------------------------------------------------------------------
section("Buses")
status, data = request("GET", "/api/buses/1")
check("GET /api/buses/1 returns 200", status, 200)
for key in ("num", "fader", "on"):
    check(f"bus has {key}", key in data, True)

status, data = request("GET", "/api/buses/0")
check("GET /api/buses/0 = 400", status, 400)

status, data = request("GET", "/api/buses/17")
check("GET /api/buses/17 = 400", status, 400)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
section("Main")
status, data = request("GET", "/api/main/st")
check("GET /api/main/st returns 200", status, 200)
for key in ("fader", "on"):
    check(f"main.st has {key}", key in data, True)

status, data = request("GET", "/api/main/m")
check("GET /api/main/m returns 200", status, 200)


# ---------------------------------------------------------------------------
# DCA
# ---------------------------------------------------------------------------
section("DCA")
status, data = request("GET", "/api/dcas/1")
check("GET /api/dcas/1 returns 200", status, 200)
for key in ("num", "fader", "on"):
    check(f"dca has {key}", key in data, True)

status, data = request("GET", "/api/dcas/0")
check("GET /api/dcas/0 = 400", status, 400)

status, data = request("GET", "/api/dcas/9")
check("GET /api/dcas/9 = 400", status, 400)


# ---------------------------------------------------------------------------
# FX
# ---------------------------------------------------------------------------
section("FX")
status, data = request("GET", "/api/fx/1")
check("GET /api/fx/1 returns 200", status, 200)
check("fx has slot", "slot" in data, True)

status, data = request("GET", "/api/fx/1/params/1")
if status == 200:
    check("fx param has value", "value" in data, True)

status, data = request("GET", "/api/fx/0")
check("GET /api/fx/0 = 400", status, 400)

status, data = request("GET", "/api/fx/9")
check("GET /api/fx/9 = 400", status, 400)


# ---------------------------------------------------------------------------
# Headamps
# ---------------------------------------------------------------------------
section("Headamps")
status, data = request("GET", "/api/headamps/0")
check("GET /api/headamps/0 returns 200", status, 200)
for key in ("gain", "phantom"):
    check(f"headamp has {key}", key in data, True)

status, data = request("GET", "/api/headamps/-1")
check("GET /api/headamps/-1 = 400", status, 400)

status, data = request("GET", "/api/headamps/128")
check("GET /api/headamps/128 = 400", status, 400)


# ---------------------------------------------------------------------------
# Scenes / Cues / Snippets
# ---------------------------------------------------------------------------
section("Scenes / Cues")
status, data = request("POST", "/api/scene/0")
check("POST /api/scene/0 returns 200", status, 200)

status, data = request("POST", "/api/scene/100")
check("POST /api/scene/100 = 400", status, 400)

status, data = request("POST", "/api/cue/0")
check("POST /api/cue/0 returns 200", status, 200)

status, data = request("POST", "/api/snippet/0")
check("POST /api/snippet/0 returns 200", status, 200)


# ---------------------------------------------------------------------------
# USB
# ---------------------------------------------------------------------------
section("USB")
status, data = request("GET", "/api/usb")
check("GET /api/usb returns 200", status, 200)
for key in ("mounted",):
    check(f"usb has {key}", key in data, True)

status, data = request("GET", "/api/usb/list")
check("GET /api/usb/list returns 200", status, 200)
check("usb list has entries", "entries" in data, True)


status, data = request("POST", "/api/usb/upload", {"filename": "test_upload.scn"})
check("POST /api/usb/upload returns 200", status, 200)
check("usb upload ok", data, {"ok": True, "filename": "test_upload.scn"})

status, data = request("POST", "/api/usb/upload", {"filename": "test_scene.scn", "scene_num": 5})
check("POST /api/usb/upload with scene_num returns 200", status, 200)
check("usb upload with scene_num ok", data, {"ok": True, "filename": "test_scene.scn"})


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------
section("404 handling")
status, _ = request("GET", "/api/nonexistent")
check("GET /api/nonexistent = 404", status, 404)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*60}")
total = pass_count + fail_count
if fail_count == 0:
    print(f"  ALL {pass_count}/{pass_count} TESTS PASSED")
else:
    print(f"  {pass_count}/{total} PASSED, {fail_count}/{total} FAILED")
print(f"{'='*60}")

sys.exit(1 if fail_count else 0)
