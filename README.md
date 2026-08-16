# Cirkit Garden (Beta)

Cirkit Garden is a Raspberry Pi garden control app built for the CirkitScape Top HAT.

It is intended to run on a Raspberry Pi with the CirkitScape Top HAT setup environment, providing browser-based garden management, pump control, sensor monitoring, scheduling, interlocks, and WiFi node support.

## Status

**Beta software for Raspberry Pi + CirkitScape Top HAT systems**

This repository is intended for:
- Raspberry Pi deployments
- CirkitScape Top HAT-based setups
- local network access
- users comfortable with wiring, GPIO, I²C/UART setup, and hands-on troubleshooting

This is **not a general-purpose cross-platform installer**.

## What it currently does

- user registration and login
- create and manage multiple gardens
- assign unique per-garden pump outputs
- support Raspberry Pi GPIO and MCP23017-based output control
- add direct sensors and WiFi nodes
- add node-attached sensors and motor / pump devices
- per-garden scheduling
- per-garden auto-water controls
- per-garden manual pump override
- per-garden interlock logic
- dashboard, system, health, and diagnostic views
- SQLite-backed persistence
- recent sensor-reading and pump-event history

## High-level architecture

Cirkit Garden is a Flask app with a server-rendered web UI.

Core pieces in this repo:
- `GardenPi.py` — main application entrypoint
- `templates/` — Jinja/Bootstrap UI templates
- `requirements.txt` — Python dependencies
- `settings.db` — SQLite database created automatically on first run

The app is built around:
- a Raspberry Pi host
- the CirkitScape Top HAT setup environment
- native Raspberry Pi GPIO
- MCP23017 GPIO expansion
- direct analog sensors
- WiFi-connected nodes such as Raspberry Pi Pico-based devices

## Intended deployment model

Recommended usage:
- run on a Raspberry Pi
- use the CirkitScape Top HAT setup script first
- access the app from your local network
- validate wiring, GPIO mappings, sensor behavior, and pump logic before unattended use

## Installation

### 1. Set up the Raspberry Pi with the CirkitScape Top HAT setup script

Use the official setup repo:

- `https://github.com/Cirkitscape/Top_HAT_Setup`

Example:

```bash
git clone https://github.com/Cirkitscape/Top_HAT_Setup.git
cd Top_HAT_Setup
chmod +x TopHAT_setup.sh
./TopHAT_setup.sh
```

That script sets up:
- required system packages
- I²C support
- UART support
- a Python virtual environment at `~/myproject/venv`
- common Python dependencies used for Top HAT / ADC work

If I²C or UART were just enabled, reboot the Pi before continuing.

Activate the created environment:

```bash
source ~/myproject/venv/bin/activate
```

### 2. Clone the Cirkit Garden repository

```bash
git clone https://github.com/Cirkitscape/Cirkit-Garden
cd CirkitGarden
```

### 3. Install Cirkit Garden Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Set environment variables

Required:

```bash
export FLASK_SECRET_KEY='replace-this-with-a-random-secret'
```

Optional for WiFi node API access:

```bash
export PICO_API_KEY='replace-this-with-your-shared-node-api-key'
```

### 5. Run the app

```bash
python GardenPi.py
```

Then open:
- `http://127.0.0.1:5000` on the Pi, or
- `http://<your-pi-lan-ip>:5000` from another device on your LAN

## First-run behavior

On first run, the app will:
- create `settings.db` automatically if it does not exist
- initialize the required SQLite tables
- start the web server on `0.0.0.0:5000`
- allow account registration from the web UI

## Environment variables

### `FLASK_SECRET_KEY`
Required.

Example:

```bash
export FLASK_SECRET_KEY='use-a-long-random-value-here'
```

### `PICO_API_KEY`
Optional shared API key for Pico/WiFi node communication.

```bash
export PICO_API_KEY='shared-key-between-hub-and-node'
```

## Dependencies

Current Python dependencies in `requirements.txt`:
- Flask
- requests

The app also attempts to use hardware-specific modules at runtime, including:
- `adc_reader`
- `mcp_gpio`
- `RPi.GPIO`

These are expected as part of the Raspberry Pi / Top HAT environment.

## Hardware notes

Cirkit Garden is structured around a Raspberry Pi garden hub with CirkitScape Top HAT support.

Examples of the current model include:
- direct analog sensor inputs
- garden-specific pump output assignments
- MCP23017 output expansion
- Raspberry Pi GPIO outputs
- WiFi node devices for remote sensing or control

Real deployment may require matching hardware wiring and node-side firmware/config depending on your attached sensors and nodes.

## Data and persistence

Cirkit Garden stores application state in a local SQLite database:
- database path: `settings.db`

This includes:
- users
- gardens
- devices
- per-garden control state
- schedules
- recent sensor readings
- recent pump events

Back up `settings.db` before major changes.

## Useful routes and pages

Main UI areas include:
- `/dashboard` — high-level garden status
- `/garden` — garden list and management
- `/schedule` — aggregate schedule view
- `/settings` — system/diagnostics page
- `/help` — basic usage help

Diagnostic endpoints:
- `/health`
- `/diag`

## Development / maintenance notes

Basic syntax check:

```bash
python -m py_compile GardenPi.py
```

Typical run flow on the Pi:

```bash
source ~/myproject/venv/bin/activate
export FLASK_SECRET_KEY='your-secret'
python GardenPi.py
```

## Known limitations

- beta-quality software
- intended for Raspberry Pi + CirkitScape Top HAT use
- not designed for public internet exposure
- no packaged installer for non-Top-HAT environments
- hardware integration details may still require customization for your exact setup
- real-world sensor calibration and watering behavior should be validated on your hardware

## Safety note

This software can control pumps and respond to sensor inputs. Test carefully before unattended use. Validate all wiring, GPIO mappings, node behavior, calibration assumptions, and interlock logic on your own hardware.

## License

This project includes an **Apache License 2.0** license. See `LICENSE` for the full text.
