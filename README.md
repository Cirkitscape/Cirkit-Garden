# Cirkit Garden (Beta)

Cirkit Garden is a self-hosted Raspberry Pi garden control app for local watering automation, sensor monitoring, and browser-based garden management.

It is designed as a maker-focused, self-hosted beta for people who want to run their own garden controller locally, manage multiple gardens from a web UI, and experiment with direct sensors, WiFi-connected nodes, schedules, pump control, and interlocks.

## Status

**Public beta / self-hosted project**

This repository is currently best suited for:
- Raspberry Pi or local Linux deployments
- LAN-only use
- users comfortable with wiring, GPIO, local networking, and hands-on troubleshooting

This is **not yet a plug-and-play consumer installer**.

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

The app is built around a Raspberry Pi host with support for:
- native Raspberry Pi GPIO
- MCP23017 GPIO expansion
- direct analog sensors
- WiFi-connected nodes such as Raspberry Pi Pico-based devices

## Intended deployment model

Recommended for now:
- run on your own Raspberry Pi with Cirkitscape Top HAT
- access it from your LAN
- use mock mode first while validating UI and behavior
- verify all wiring, thresholds, and automation behavior before relying on unattended watering

## Quick start

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd CirkitGarden
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Set environment variables

Minimum recommended setup for a first run:

```bash
export FLASK_SECRET_KEY='replace-this-with-a-random-secret'
export GARDENPI_MOCK=1
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
- `http://127.0.0.1:5000` on the same machine, or
- `http://<your-pi-lan-ip>:5000` from another device on your LAN

## First-run behavior

On first run, the app will:
- create `settings.db` automatically if it does not exist
- initialize the required SQLite tables
- start the web server on `0.0.0.0:5000`
- allow account registration from the web UI

## Environment variables

### `FLASK_SECRET_KEY`
Required for any real deployment.

Example:

```bash
export FLASK_SECRET_KEY='use-a-long-random-value-here'
```

### `GARDENPI_MOCK`
Set to `1` to force mock mode.

```bash
export GARDENPI_MOCK=1
```

Mock mode is recommended for:
- development on non-Pi systems
- UI testing
- source evaluation before hardware is wired up

### `PICO_API_KEY`
Optional shared API key for Pico/WiFi node communication.

```bash
export PICO_API_KEY='shared-key-between-hub-and-node'
```

## Dependencies

Current Python dependencies in `requirements.txt`:
- Flask
- requests

The app also attempts to use optional hardware-specific modules at runtime when not in mock mode, including:
- `adc_reader`
- `mcp_gpio`
- `RPi.GPIO`

Those hardware modules are **not required for mock mode**.

If the target hardware modules are unavailable, run with:

```bash
export GARDENPI_MOCK=1
```

## Hardware notes

Cirkit Garden is structured around a Raspberry Pi garden hub with optional expansion and remote-node support.

Examples of the current model include:
- direct analog sensor inputs
- garden-specific pump output assignments
- MCP23017 output expansion
- Raspberry Pi GPIO outputs
- WiFi node devices for remote sensing or control

Real hardware deployment may require you to provide or adapt:
- an ADC reader implementation
- MCP23017 GPIO helper support
- Raspberry Pi GPIO access on the target host
- matching firmware/config for any WiFi-connected node devices

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

## Development notes

Basic syntax check:

```bash
python -m py_compile GardenPi.py
```

Common local dev flow:

```bash
export FLASK_SECRET_KEY='dev-secret'
export GARDENPI_MOCK=1
python GardenPi.py
```

## Known limitations

- beta-quality software
- optimized for local/self-hosted use, not public internet exposure
- no packaged installer yet
- no containerized deployment included yet
- hardware integration details may require customization for your setup
- diagnostics/system views are oriented toward operator visibility and development
- real-world sensor calibration and watering behavior should be validated on your hardware

## Roadmap direction

Areas that would make public self-hosting easier:
- packaged installer or setup script
- sample `.env` file
- systemd service example
- clearer hardware integration docs
- repeatable staging/test flow
- optional containerized development workflow

## Safety note

This software can control pumps and respond to sensor inputs. Test carefully before unattended use. Validate all wiring, GPIO mappings, node behavior, calibration assumptions, and interlock logic on your own hardware.

## License

This project includes an **AGPL-3.0** license. See `LICENSE` for the full text.
