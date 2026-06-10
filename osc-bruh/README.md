# osc-bruh

Python tools for controlling Behringer X32 / Midas M32 digital mixers over OSC.

## Components

- **`x32.py`** — Core OSC library with full protocol support (channels, EQ, dynamics, buses, FX, scenes, USB recorder, headamps)
- **`x32_rest_api.py`** — FastAPI-based REST API wrapping the OSC library, with Swagger docs, WebSocket support, and CORS
- **`x32_menu.py`** — Interactive CLI menu for mixer control and USB browser/player
- **`x32_usb_list.py`** — USB directory listing utility

## Quick Start

```bash
pip install .
python x32_rest_api.py --mixer 192.168.0.108
```

Or with Docker:

```bash
docker compose up
```

Then open `http://localhost:8080/docs` for the Swagger UI.

## USB Type-B (MIDI SysEx)

Write-only control via `amidi`:

```bash
python3 x32.py midi:list
python3 x32_menu.py midi:hw:2,0,0
```

## Requirements

- Python >= 3.11
- For USB control: `alsa-utils` (`amidi`)
