#!/usr/bin/env python3
import os
import json
import atexit
import sqlite3
import random
import ipaddress
from datetime import datetime, timedelta
from threading import Lock

try:
    import requests
except Exception:
    requests = None

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------- Configuration ----------------
MOCK = os.environ.get("GARDENPI_MOCK", "0") == "1"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "settings.db")

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")

# ---------------- Constants ----------------
CHANNELS = [0, 1, 2]

TYPE_DISABLED = "disabled"
TYPE_MOISTURE = "moisture"  # legacy alias
TYPE_MOISTURE_RESISTIVE = "moisture_resistive"
TYPE_MOISTURE_CAPACITIVE = "moisture_capacitive"
TYPE_LEVEL = "level"
TYPE_WATER_LEVEL = "water_level"
TYPE_LIQUID = "liquid"
TYPE_TEMPERATURE = "temperature"
TYPE_DHT11 = "dht11"
MOISTURE_SENSOR_TYPES = {TYPE_MOISTURE, TYPE_MOISTURE_RESISTIVE, TYPE_MOISTURE_CAPACITIVE}
VALID_TYPES = {
    TYPE_DISABLED,
    TYPE_MOISTURE,
    TYPE_MOISTURE_RESISTIVE,
    TYPE_MOISTURE_CAPACITIVE,
    TYPE_LEVEL,
    TYPE_WATER_LEVEL,
    TYPE_LIQUID,
    TYPE_TEMPERATURE,
    TYPE_DHT11,
}
SENSOR_TYPE_LABELS = {
    TYPE_DISABLED: "Not assigned",
    TYPE_MOISTURE: "Resistive Soil Moisture Sensor",
    TYPE_MOISTURE_RESISTIVE: "Resistive Soil Moisture Sensor",
    TYPE_MOISTURE_CAPACITIVE: "Capacitive Soil Moisture Sensor",
    TYPE_LEVEL: "IR Liquid Sensor",
    TYPE_WATER_LEVEL: "Water Level Sensor",
    TYPE_LIQUID: "Liquid Sensor",
    TYPE_TEMPERATURE: "Temperature Sensor",
    TYPE_DHT11: "DHT11 Temperature / Humidity Sensor",
}

DEVICE_PI = "rpi"
DEVICE_PICO = "pico"
DEVICE_SENSOR = "sensor"
DEVICE_MOTOR = "motor"
DEVICE_UNASSIGNED = "unassigned"
VALID_DEVICE_TYPES = {DEVICE_PI, DEVICE_PICO, DEVICE_SENSOR, DEVICE_MOTOR, DEVICE_UNASSIGNED}
WIFI_NODE_DEVICE_TYPES = {DEVICE_PI, DEVICE_PICO}
DEVICE_TYPE_LABELS = {
    DEVICE_PI: "Raspberry Pi Zero-5 with Top HAT",
    DEVICE_PICO: "Raspberry Pi Pico",
    DEVICE_SENSOR: "Direct Sensor",
    DEVICE_MOTOR: "Motor / Pump",
    DEVICE_UNASSIGNED: "Not assigned",
}

PICO_HTTP_TIMEOUT_S = 2.0
PICO_API_KEY = os.environ.get("PICO_API_KEY", "change-me")
PICO_VALID_GPIO_PINS = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 26, 27, 28}
PICO_OUTPUT_PINS = {15, 16, 17, 18, 19, 20, 21, 22}
PICO_SENSOR_PINS = {26, 27, 28}
RPI_TOP_HAT_ADC_CHANNELS = {0, 1, 2}

LOCATION_INDOOR = "indoor"
LOCATION_OUTDOOR = "outdoor"
VALID_LOCATIONS = {LOCATION_INDOOR, LOCATION_OUTDOOR}

RESISTIVE_SOIL_WET_V = 1.31
RESISTIVE_SOIL_DRY_V = 1.184
RESISTIVE_SOIL_PUMP_ON_V = 1.25
CAPACITIVE_SOIL_WET_V = 1.31
CAPACITIVE_SOIL_DRY_V = 1.184
CAPACITIVE_SOIL_PUMP_ON_V = 1.25
SOIL_WET_V = RESISTIVE_SOIL_WET_V
SOIL_DRY_V = RESISTIVE_SOIL_DRY_V
SOIL_PUMP_ON_V = RESISTIVE_SOIL_PUMP_ON_V

IR_WATER_PRESENT_MAX_V = 1.19
IR_NEEDS_REFILL_MIN_V = 1.20

PUMP_BIT = 0
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
VALID_SCHEDULE_PUMP_MODES = {"on", "off"}
DEFAULT_GARDEN_SCHEDULE_DAYS_JSON = json.dumps({d: False for d in DAY_KEYS})
PUMP_CONTROLLER_PI_GPIO = "pi_gpio"
PUMP_CONTROLLER_MCP23017 = "mcp23017"
VALID_PUMP_CONTROLLERS = {PUMP_CONTROLLER_PI_GPIO, PUMP_CONTROLLER_MCP23017}
VALID_PUMP_MCP_BANKS = {"A", "B"}
SENSOR_LOG_MIN_INTERVAL_S = 300
TOP_HAT_ADC_VOLTAGE_OFFSET = 1.244
DEFAULT_HISTORY_RANGE = "24h"
VALID_HISTORY_RANGES = {
    "1h": {"label": "Last 1 hour", "hours": 1},
    "6h": {"label": "Last 6 hours", "hours": 6},
    "24h": {"label": "Last 24 hours", "hours": 24},
    "7d": {"label": "Last 7 days", "hours": 24 * 7},
    "all": {"label": "All logged data", "hours": None},
}
MAX_HISTORY_POINTS = 1500

GARDEN_SCHEMA_ADDITIONS = (
    ("pump_controller", "TEXT NOT NULL DEFAULT 'mcp23017'"),
    ("pump_pi_gpio", "INTEGER"),
    ("pump_mcp_bank", "TEXT"),
    ("pump_mcp_bit", "INTEGER"),
    ("pump_manual_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("pump_manual_requested_on", "INTEGER NOT NULL DEFAULT 0"),
    ("auto_water_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("interlock_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("schedule_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("schedule_start", "TEXT NOT NULL DEFAULT '06:00:00'"),
    ("schedule_stop", "TEXT NOT NULL DEFAULT '06:15:00'"),
    ("schedule_pump_mode", "TEXT NOT NULL DEFAULT 'on'"),
    ("schedule_autowater_enabled", "INTEGER NOT NULL DEFAULT 0"),
    (
        "schedule_days_json",
        f"TEXT NOT NULL DEFAULT '{DEFAULT_GARDEN_SCHEDULE_DAYS_JSON}'",
    ),
)

# ---------------- Globals ----------------
gpio_lock = Lock()
current_outputs = {"A": 0x00, "B": 0x00}
pi_output_states = {}
hw_errors = []
sensor_log_cache = {}
pump_event_cache = {}

manual_pump_override = {
    "enabled": False,
    "requested_on": False,
}

adc_reader = None
mcp_gpio = None
pi_gpio = None


# ---------------- Response headers ----------------
@app.after_request
def add_no_cache_headers(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ---------------- DB connection helper ----------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------- Hardware helpers ----------------
def _log_hw_error(msg: str):
    hw_errors.append(msg)


if not MOCK:
    try:
        import adc_reader as _adc_reader
        adc_reader = _adc_reader
    except Exception as e:
        _log_hw_error(f"adc_reader import failed: {e}")
        adc_reader = None

    try:
        import mcp_gpio as _mcp_gpio
        mcp_gpio = _mcp_gpio
    except Exception as e:
        _log_hw_error(f"mcp_gpio import failed: {e}")
        mcp_gpio = None

    try:
        import RPi.GPIO as _pi_gpio
        pi_gpio = _pi_gpio
    except Exception as e:
        _log_hw_error(f"RPi.GPIO import failed: {e}")
        pi_gpio = None

    if adc_reader is None:
        MOCK = True


def init_hardware():
    try:
        if mcp_gpio is not None:
            mcp_gpio.setup_gpio(dir_a=0x00, dir_b=0x00)
            mcp_gpio.write_outputs(current_outputs["A"], current_outputs["B"])

        if pi_gpio is not None:
            pi_gpio.setwarnings(False)
            pi_gpio.setmode(pi_gpio.BCM)
            for pin, is_on in list(pi_output_states.items()):
                pi_gpio.setup(pin, pi_gpio.OUT, initial=pi_gpio.HIGH if is_on else pi_gpio.LOW)
    except Exception as e:
        _log_hw_error(f"init_hardware error: {e}")
        raise


def _write_mcp_outputs():
    if MOCK or mcp_gpio is None:
        return
    mcp_gpio.write_outputs(current_outputs["A"], current_outputs["B"])


def _ensure_pi_output_ready(pin: int):
    if MOCK or pi_gpio is None:
        return
    pi_gpio.setwarnings(False)
    pi_gpio.setmode(pi_gpio.BCM)
    initial = pi_gpio.HIGH if pi_output_states.get(pin, False) else pi_gpio.LOW
    pi_gpio.setup(pin, pi_gpio.OUT, initial=initial)


def _write_pi_output(pin: int, on: bool):
    if MOCK or pi_gpio is None:
        return
    _ensure_pi_output_ready(pin)
    pi_gpio.output(pin, pi_gpio.HIGH if on else pi_gpio.LOW)


def get_pump_output_state(target=None):
    normalized = _normalize_garden_pump_target(target, use_legacy_defaults=True)
    with gpio_lock:
        if normalized["controller"] == PUMP_CONTROLLER_PI_GPIO:
            pin = normalized["pin"]
            return bool(pi_output_states.get(pin, False)) if pin is not None else False

        bank = normalized["bank"]
        bit = normalized["bit"]
        if bank not in VALID_PUMP_MCP_BANKS or bit is None:
            return False
        return bool(current_outputs[bank] & (1 << bit))


def set_pump_output(target, on: bool):
    normalized = _normalize_garden_pump_target(target, use_legacy_defaults=True)
    with gpio_lock:
        try:
            if normalized["controller"] == PUMP_CONTROLLER_PI_GPIO:
                pin = normalized["pin"]
                if pin is None:
                    raise ValueError("Pi GPIO pump target is missing a BCM pin")
                pi_output_states[pin] = bool(on)
                _write_pi_output(pin, on)
                return normalized

            bank = normalized["bank"]
            bit = normalized["bit"]
            if bank not in VALID_PUMP_MCP_BANKS or bit is None:
                raise ValueError("MCP23017 pump target is incomplete")

            mask = 1 << bit
            if on:
                current_outputs[bank] |= mask
            else:
                current_outputs[bank] &= ~mask
            _write_mcp_outputs()
            return normalized
        except Exception as e:
            _log_hw_error(f"set_pump_output error ({normalized}): {e}")
            raise


def set_pump(on: bool, target=None):
    set_pump_output(target if target is not None else default_garden_pump_target(), on)


def clear_pump(target=None):
    set_pump(False, target=target)


def set_manual_pump_override(enabled: bool, requested_on: bool = False):
    global manual_pump_override
    manual_pump_override = {
        "enabled": bool(enabled),
        "requested_on": bool(requested_on),
    }


def get_manual_pump_override():
    return {
        "enabled": manual_pump_override["enabled"],
        "requested_on": manual_pump_override["requested_on"],
    }


def cleanup_hardware():
    try:
        clear_pump()
        for pin in list(pi_output_states):
            try:
                clear_pump({"controller": PUMP_CONTROLLER_PI_GPIO, "pin": pin})
            except Exception as pin_error:
                _log_hw_error(f"cleanup_hardware pi clear error on BCM {pin}: {pin_error}")
        if not MOCK and mcp_gpio and hasattr(mcp_gpio, "cleanup"):
            mcp_gpio.cleanup()
        if not MOCK and pi_gpio and hasattr(pi_gpio, "cleanup"):
            pi_gpio.cleanup()
    except Exception as e:
        _log_hw_error(f"cleanup_hardware error: {e}")


atexit.register(cleanup_hardware)

try:
    init_hardware()
except Exception:
    MOCK = True


# ---------------- ADC helpers ----------------
def read_all_channels_once():
    if MOCK or adc_reader is None:
        base = [1.30, 1.15, 1.22]
        wobble = [b + (random.random() - 0.5) * 0.02 for b in base]
        return {
            "AIN0": round(wobble[0], 4),
            "AIN1": round(wobble[1], 4),
            "AIN2": round(wobble[2], 4),
            "AIN3": 0.0,
        }

    try:
        vals = adc_reader.read_all_channels()
        return {k: float(v) for k, v in vals.items()}
    except Exception as e:
        _log_hw_error(f"ADC read error: {e}")
        return {
            "AIN0": 0.0,
            "AIN1": 0.0,
            "AIN2": 0.0,
            "AIN3": 0.0,
        }


def read_voltage(ch: int) -> float:
    vals = read_all_channels_once()
    try:
        return float(vals.get(f"AIN{ch}", 0.0))
    except Exception:
        return 0.0


def top_hat_output_voltage(raw_voltage):
    try:
        return float(raw_voltage) - TOP_HAT_ADC_VOLTAGE_OFFSET
    except Exception:
        return -TOP_HAT_ADC_VOLTAGE_OFFSET


def now_iso_timestamp():
    return datetime.now().isoformat(timespec="seconds")


def is_interlock_sensor_type(sensor_type) -> bool:
    return sensor_type in {TYPE_LEVEL, TYPE_WATER_LEVEL}


def normalize_sensor_type(sensor_type):
    sensor_type = str(sensor_type or TYPE_DISABLED).strip().lower()
    if sensor_type == TYPE_MOISTURE:
        return TYPE_MOISTURE_RESISTIVE
    return sensor_type


def is_moisture_sensor_type(sensor_type) -> bool:
    return normalize_sensor_type(sensor_type) in {TYPE_MOISTURE_RESISTIVE, TYPE_MOISTURE_CAPACITIVE}


def moisture_sensor_profile(sensor_type):
    normalized = normalize_sensor_type(sensor_type)
    if normalized == TYPE_MOISTURE_CAPACITIVE:
        return {
            "sensor_type": TYPE_MOISTURE_CAPACITIVE,
            "label": SENSOR_TYPE_LABELS[TYPE_MOISTURE_CAPACITIVE],
            "wet_v": CAPACITIVE_SOIL_WET_V,
            "dry_v": CAPACITIVE_SOIL_DRY_V,
            "pump_on_v": CAPACITIVE_SOIL_PUMP_ON_V,
        }
    return {
        "sensor_type": TYPE_MOISTURE_RESISTIVE,
        "label": SENSOR_TYPE_LABELS[TYPE_MOISTURE_RESISTIVE],
        "wet_v": RESISTIVE_SOIL_WET_V,
        "dry_v": RESISTIVE_SOIL_DRY_V,
        "pump_on_v": RESISTIVE_SOIL_PUMP_ON_V,
    }


def sensor_type_requires_analog_input(sensor_type) -> bool:
    return is_moisture_sensor_type(sensor_type) or normalize_sensor_type(sensor_type) in {TYPE_LEVEL, TYPE_LIQUID, TYPE_TEMPERATURE}


def sensor_type_requires_gpio_input(sensor_type) -> bool:
    return sensor_type in {TYPE_WATER_LEVEL, TYPE_DHT11}


def _coerce_float(value):
    try:
        return float(value)
    except Exception:
        return None


def _coerce_int(value):
    try:
        return int(value)
    except Exception:
        return None


def _coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "on", "yes", "high", "wet", "water", "full", "ok"}:
            return True
        if normalized in {"0", "false", "off", "no", "low", "dry", "empty", "refill"}:
            return False
    return None


def _iter_node_payload_candidates(payload):
    if not isinstance(payload, dict):
        return

    containers = [
        payload,
        payload.get("sensors"),
        payload.get("adc"),
        payload.get("gpio"),
        payload.get("inputs"),
        payload.get("dht"),
        payload.get("dht11"),
        payload.get("water"),
        payload.get("water_level"),
        payload.get("level"),
    ]
    seen_ids = set()
    for container in containers:
        if not isinstance(container, dict):
            continue
        container_id = id(container)
        if container_id in seen_ids:
            continue
        seen_ids.add(container_id)
        yield (None, container)
        for key, value in container.items():
            if isinstance(value, dict):
                yield (str(key), value)


def _candidate_matches_sensor_binding(key, candidate, gpio_pin, node_type):
    gpio_pin = _coerce_int(gpio_pin)
    if gpio_pin is None:
        return False

    for field in ("pin", "gpio", "gpio_pin", "gp"):
        if _coerce_int(candidate.get(field)) == gpio_pin:
            return True

    if node_type == DEVICE_PI:
        for field in ("channel", "adc", "ain"):
            if _coerce_int(candidate.get(field)) == gpio_pin:
                return True

    key = (key or "").strip().lower()
    possible_keys = {
        str(gpio_pin),
        f"pin:{gpio_pin}",
        f"gpio:{gpio_pin}",
        f"gp{gpio_pin}",
        f"gpio{gpio_pin}",
    }
    if node_type == DEVICE_PI:
        possible_keys.update({f"ain{gpio_pin}", f"channel:{gpio_pin}", f"adc{gpio_pin}"})
    return key in possible_keys


def normalize_node_sensor_reading(payload, sensor_type, gpio_pin, node_type):
    sensor_type = normalize_sensor_type(sensor_type)
    gpio_pin = _coerce_int(gpio_pin)
    node_type = str(node_type or "").strip().lower()
    best = None

    for key, candidate in _iter_node_payload_candidates(payload):
        if not isinstance(candidate, dict):
            continue
        if _candidate_matches_sensor_binding(key, candidate, gpio_pin, node_type):
            best = candidate
            break
        if best is None:
            if sensor_type == TYPE_DHT11 and any(
                field in candidate for field in ("temperature", "temperature_c", "temp_c", "humidity")
            ):
                best = candidate
            elif sensor_type == TYPE_WATER_LEVEL and any(
                field in candidate for field in ("wet", "water_present", "level_percent", "state", "raw")
            ):
                best = candidate

    if best is None and isinstance(payload, dict):
        best = payload
    if not isinstance(best, dict):
        best = {}

    if sensor_type == TYPE_DHT11:
        temp_c = _coerce_float(best.get("temperature_c"))
        if temp_c is None:
            temp_c = _coerce_float(best.get("temp_c"))
        if temp_c is None:
            temp_c = _coerce_float(best.get("temperature"))
        humidity = _coerce_float(best.get("humidity"))
        state = "OK" if temp_c is not None or humidity is not None else (best.get("state") or "NO_DATA")
        label_parts = []
        if temp_c is not None:
            label_parts.append(f"{temp_c:.1f} C")
        if humidity is not None:
            label_parts.append(f"{humidity:.1f}% RH")
        label = " / ".join(label_parts) or best.get("label") or best.get("status") or "No DHT11 data"
        return {
            "voltage": None,
            "state": state,
            "label": label,
            "temperature_c": temp_c,
            "humidity": humidity,
            "raw": best.get("raw"),
        }

    if sensor_type == TYPE_WATER_LEVEL:
        level_percent = _coerce_float(best.get("level_percent"))
        raw_value = best.get("raw") if best.get("raw") is not None else best.get("value")
        wet = _coerce_bool(best.get("wet"))
        if wet is None:
            wet = _coerce_bool(best.get("water_present"))
        state = best.get("state")
        label = best.get("label") or best.get("status")
        if wet is not None:
            state = "WATER_PRESENT" if wet else "NEEDS_REFILL"
            label = label or ("Water present" if wet else "Needs refill")
        elif level_percent is not None:
            state = state or ("WATER_PRESENT" if level_percent > 0 else "NEEDS_REFILL")
            label = label or f"{level_percent:.1f}%"
        else:
            state = state or "UNKNOWN"
            label = label or (f"Raw {raw_value}" if raw_value is not None else "No water level data")
        return {
            "voltage": _coerce_float(best.get("voltage")),
            "state": state,
            "label": label,
            "level_percent": level_percent,
            "raw": raw_value,
        }

    result = dict(best) if isinstance(best, dict) else {}
    voltage = _coerce_float(result.get("voltage"))
    if voltage is not None:
        result["voltage"] = round(voltage, 3)
    if is_moisture_sensor_type(sensor_type):
        wet_percent = _coerce_float(result.get("moisture_percent"))
        if wet_percent is None:
            wet_percent = _coerce_float(result.get("wet_percent"))
        if wet_percent is not None:
            result["wet_percent"] = wet_percent
            result.setdefault("label", f"{wet_percent:.1f}% wet")
    return result


def _sensor_status_text(sensor_type, reading):
    if not isinstance(reading, dict):
        return "No live data"
    if is_moisture_sensor_type(sensor_type):
        wet = _coerce_float(reading.get("wet_percent"))
        if wet is None:
            wet = _coerce_float(reading.get("moisture_percent"))
        if wet is not None:
            return f"{wet:.1f}% wet"
    return str(reading.get("label") or reading.get("state") or reading.get("status") or (f"Raw {reading.get('raw')}" if reading.get("raw") is not None else "Active"))


def _sensor_voltage_text(reading):
    if not isinstance(reading, dict):
        return "--"
    voltage = _coerce_float(reading.get("voltage"))
    if voltage is None:
        return "--"
    return f"{voltage:.3f}"


def _node_sensor_binding_label(node_type, gpio_pin, sensor_type=None, device_type=None):
    pin = _coerce_int(gpio_pin)
    if pin is None:
        return "--"
    node_type = str(node_type or "").strip().lower()
    sensor_type = str(sensor_type or "").strip().lower()
    device_type = str(device_type or "").strip().lower()

    if node_type == DEVICE_PI:
        use_analog_label = device_type == DEVICE_SENSOR and sensor_type_requires_analog_input(sensor_type)
        return f"AIN{pin}" if use_analog_label else f"GPIO {pin}"
    return f"GP{pin}"


def _sensor_log_signature(reading):
    if not isinstance(reading, dict):
        return None
    voltage_raw = reading.get("voltage")
    if voltage_raw is None:
        voltage = None
    else:
        try:
            voltage = round(float(voltage_raw), 2)
        except Exception:
            voltage = None
    wet_percent_raw = reading.get("wet_percent")
    if wet_percent_raw is None:
        wet_percent = None
    else:
        try:
            wet_percent = round(float(wet_percent_raw), 1)
        except Exception:
            wet_percent = None
    return (
        voltage,
        wet_percent,
        reading.get("state"),
        reading.get("label"),
        bool(reading.get("pump_request", False)),
    )


def log_sensor_reading(garden_id, channel, sensor_type, reading):
    if not garden_id or not isinstance(reading, dict):
        return False
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO sensor_readings (
                garden_id, channel, sensor_type, voltage, wet_percent,
                state, label, pump_request, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(garden_id),
                int(channel),
                sensor_type,
                reading.get("voltage"),
                reading.get("wet_percent"),
                reading.get("state"),
                reading.get("label"),
                int(bool(reading.get("pump_request", False))),
                now_iso_timestamp(),
            ),
        )
    return True


def log_pump_event(garden_id, result):
    if not garden_id or not isinstance(result, dict):
        return False
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO pump_events (
                garden_id, pump_state, pump_reason, pump_target_json,
                output_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(garden_id),
                result.get("pump_state", "OFF"),
                result.get("pump_reason", "UNKNOWN"),
                json.dumps(result.get("pump_target") or {}),
                int(bool(result.get("pump_output_active", False))),
                now_iso_timestamp(),
            ),
        )
    return True


def maybe_log_garden_status(garden, stat):
    if not isinstance(garden, dict) or not isinstance(stat, dict):
        return
    garden_id = garden.get("id")
    if not garden_id:
        return

    now = datetime.now()
    for ch, info in (stat.get("channels") or {}).items():
        if not info.get("enabled"):
            continue
        reading = info.get("reading")
        signature = _sensor_log_signature(reading)
        if signature is None:
            continue
        cache_key = (int(garden_id), int(ch), info.get("type"))
        cached = sensor_log_cache.get(cache_key)
        should_log = cached is None
        if not should_log and cached.get("signature") != signature:
            should_log = True
        if not should_log and (now - cached.get("timestamp", now)).total_seconds() >= SENSOR_LOG_MIN_INTERVAL_S:
            should_log = True
        if not should_log:
            continue
        try:
            if log_sensor_reading(garden_id, ch, info.get("type"), reading):
                sensor_log_cache[cache_key] = {"signature": signature, "timestamp": now}
        except Exception as e:
            _log_hw_error(f"sensor_reading log error: {e}")

    result = {
        "pump_state": stat.get("pump_state", "OFF"),
        "pump_reason": stat.get("pump_reason", "UNKNOWN"),
        "pump_target": stat.get("pump_target") or {},
        "pump_output_active": bool(stat.get("pump_output_active", False)),
    }
    pump_signature = (
        result["pump_state"],
        result["pump_reason"],
        json.dumps(result["pump_target"], sort_keys=True),
        result["pump_output_active"],
    )
    pump_cache_key = int(garden_id)
    if pump_event_cache.get(pump_cache_key) == pump_signature:
        return
    try:
        if log_pump_event(garden_id, result):
            pump_event_cache[pump_cache_key] = pump_signature
    except Exception as e:
        _log_hw_error(f"pump_event log error: {e}")


# ---------------- Database ----------------
def _ensure_garden_schema(conn):
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(gardens)").fetchall()
    }
    for column_name, column_sql in GARDEN_SCHEMA_ADDITIONS:
        if column_name in existing_columns:
            continue
        conn.execute(f"ALTER TABLE gardens ADD COLUMN {column_name} {column_sql}")

    existing_device_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(devices)").fetchall()
    }
    for column_name, column_sql in [
        ("node_parent_key", "TEXT"),
        ("gpio_pin", "INTEGER"),
    ]:
        if column_name in existing_device_columns:
            continue
        conn.execute(f"ALTER TABLE devices ADD COLUMN {column_name} {column_sql}")


def init_db():
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                config_json TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS gardens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT NOT NULL,
                location TEXT NOT NULL,
                device_type TEXT NOT NULL,
                device_label TEXT,
                ip_address TEXT,
                sensor_type TEXT NOT NULL,
                channel INTEGER NOT NULL,
                sensor_name TEXT,
                pump_controller TEXT NOT NULL DEFAULT 'mcp23017',
                pump_pi_gpio INTEGER,
                pump_mcp_bank TEXT,
                pump_mcp_bit INTEGER,
                pump_manual_enabled INTEGER NOT NULL DEFAULT 0,
                pump_manual_requested_on INTEGER NOT NULL DEFAULT 0,
                auto_water_enabled INTEGER NOT NULL DEFAULT 0,
                interlock_enabled INTEGER NOT NULL DEFAULT 1,
                schedule_enabled INTEGER NOT NULL DEFAULT 0,
                schedule_start TEXT NOT NULL DEFAULT '06:00:00',
                schedule_stop TEXT NOT NULL DEFAULT '06:15:00',
                schedule_pump_mode TEXT NOT NULL DEFAULT 'on',
                schedule_autowater_enabled INTEGER NOT NULL DEFAULT 0,
                schedule_days_json TEXT NOT NULL DEFAULT '{DEFAULT_GARDEN_SCHEDULE_DAYS_JSON}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                garden_id INTEGER NOT NULL,
                device_type TEXT NOT NULL,
                device_label TEXT,
                location TEXT NOT NULL,
                ip_address TEXT,
                sensor_type TEXT NOT NULL,
                channel INTEGER NOT NULL,
                sensor_name TEXT,
                node_parent_key TEXT,
                gpio_pin INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(garden_id) REFERENCES gardens(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sensor_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                garden_id INTEGER NOT NULL,
                channel INTEGER NOT NULL,
                sensor_type TEXT NOT NULL,
                voltage REAL,
                wet_percent REAL,
                state TEXT,
                label TEXT,
                pump_request INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(garden_id) REFERENCES gardens(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pump_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                garden_id INTEGER NOT NULL,
                pump_state TEXT NOT NULL,
                pump_reason TEXT NOT NULL,
                pump_target_json TEXT,
                output_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(garden_id) REFERENCES gardens(id) ON DELETE CASCADE
            )
            """
        )
        _ensure_garden_schema(conn)


init_db()


# ---------------- User helpers ----------------
def create_user(username: str, password: str) -> bool:
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, generate_password_hash(password)),
            )
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception:
        return False


def get_user(username: str):
    try:
        with get_db_connection() as conn:
            cur = conn.execute(
                "SELECT id, username, password_hash FROM users WHERE username = ?",
                (username,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "username": row[1],
                "password_hash": row[2],
            }
    except Exception:
        return None


# ---------------- Settings helpers ----------------
def default_schedule():
    return {
        "enabled": False,
        "start": "06:00:00",
        "stop": "06:15:00",
        "days": {
            "mon": True,
            "tue": True,
            "wed": True,
            "thu": True,
            "fri": True,
            "sat": False,
            "sun": False,
        },
        "pump_mode": "on",
        "autowater_enabled": False,
    }


def default_config():
    return {
        "channels": {
            ch: {
                "enabled": (ch in (0, 1)),
                "type": TYPE_MOISTURE_RESISTIVE if ch == 0 else (TYPE_LEVEL if ch == 1 else TYPE_DISABLED),
            }
            for ch in CHANNELS
        },
        "schedule": default_schedule(),
        "auto_water_enabled": False,
        "pump_target": default_garden_pump_target(),
        "manual_override": {"enabled": False, "requested_on": False},
    }


def _normalize_manual_override(obj):
    if not isinstance(obj, dict):
        return {"enabled": False, "requested_on": False}
    return {
        "enabled": bool(obj.get("enabled", False)),
        "requested_on": bool(obj.get("requested_on", False)),
    }


def _normalize_schedule(obj):
    base = default_schedule()
    if not isinstance(obj, dict):
        return base

    base["enabled"] = bool(obj.get("enabled", False))
    base["start"] = str(obj.get("start", "06:00:00"))
    base["stop"] = str(obj.get("stop", "06:15:00"))

    pump_mode = str(obj.get("pump_mode", "on")).lower()
    if pump_mode not in VALID_SCHEDULE_PUMP_MODES:
        pump_mode = "on"
    base["pump_mode"] = pump_mode
    base["autowater_enabled"] = bool(obj.get("autowater_enabled", False))

    days = obj.get("days", {})
    if isinstance(days, dict):
        for d in DAY_KEYS:
            base["days"][d] = bool(days.get(d, base["days"][d]))

    return base


def _normalize_cfg(obj):
    default = default_config()
    if not isinstance(obj, dict):
        return default

    norm_channels = {}
    raw_channels = obj.get("channels", obj)
    if isinstance(raw_channels, dict):
        for k, v in raw_channels.items():
            try:
                ik = int(k)
            except Exception:
                continue

            typ = normalize_sensor_type((v or {}).get("type", TYPE_DISABLED))
            if typ not in VALID_TYPES:
                typ = TYPE_DISABLED

            norm_channels[ik] = {
                "enabled": bool((v or {}).get("enabled", False)),
                "type": typ,
            }

    for ch in CHANNELS:
        norm_channels.setdefault(ch, {"enabled": False, "type": TYPE_DISABLED})

    return {
        "channels": norm_channels,
        "schedule": _normalize_schedule(obj.get("schedule", {})),
        "auto_water_enabled": bool(obj.get("auto_water_enabled", False)),
        "pump_target": _normalize_garden_pump_target(
            obj.get("pump_target"),
            use_legacy_defaults=True,
        ),
        "manual_override": _normalize_manual_override(obj.get("manual_override")),
    }


def load_settings(user_id: int):
    try:
        with get_db_connection() as conn:
            cur = conn.execute(
                "SELECT config_json FROM settings WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return default_config()
            return _normalize_cfg(json.loads(row[0]))
    except Exception:
        return default_config()


def save_settings(user_id: int, cfg: dict):
    data = json.dumps(
        {
            "channels": {str(k): v for k, v in cfg["channels"].items()},
            "schedule": cfg["schedule"],
            "auto_water_enabled": cfg["auto_water_enabled"],
            "pump_target": _normalize_garden_pump_target(
                cfg.get("pump_target"),
                use_legacy_defaults=True,
            ),
            "manual_override": _normalize_manual_override(cfg.get("manual_override")),
        }
    )

    with get_db_connection() as conn:
        cur = conn.execute(
            "UPDATE settings SET config_json=? WHERE user_id=?",
            (data, user_id),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO settings (user_id, config_json) VALUES (?, ?)",
                (user_id, data),
            )


def get_current_config():
    uid = session.get("user_id")
    if uid:
        return load_settings(uid)
    return _normalize_cfg(session.get("guest_cfg", default_config()))


def save_current_config(cfg):
    uid = session.get("user_id")
    if uid:
        save_settings(uid, cfg)
    else:
        session["guest_cfg"] = {
            "channels": {str(k): v for k, v in cfg["channels"].items()},
            "schedule": cfg["schedule"],
            "auto_water_enabled": cfg["auto_water_enabled"],
            "pump_target": _normalize_garden_pump_target(
                cfg.get("pump_target"),
                use_legacy_defaults=True,
            ),
            "manual_override": _normalize_manual_override(cfg.get("manual_override")),
        }
        session.modified = True


def build_legacy_cycle_config(cfg_snapshot=None):
    cfg = _normalize_cfg(cfg_snapshot if cfg_snapshot is not None else get_current_config())
    cfg["pump_target"] = default_garden_pump_target()
    cfg["manual_override"] = get_manual_pump_override()
    return cfg


# ---------------- Garden helpers ----------------
def _coerce_int_or_none(value):
    try:
        return int(value)
    except Exception:
        return None


def default_garden_pump_target():
    return {
        "controller": PUMP_CONTROLLER_MCP23017,
        "pin": None,
        "bank": "A",
        "bit": PUMP_BIT,
    }


def _normalize_garden_pump_target(target, *, use_legacy_defaults=False):
    base = default_garden_pump_target()
    if not isinstance(target, dict):
        return dict(base) if use_legacy_defaults else {
            "controller": base["controller"],
            "pin": None,
            "bank": None,
            "bit": None,
        }

    controller = str(target.get("controller", base["controller"])).strip().lower()
    if controller not in VALID_PUMP_CONTROLLERS:
        controller = base["controller"]

    if controller == PUMP_CONTROLLER_PI_GPIO:
        return {
            "controller": controller,
            "pin": _coerce_int_or_none(target.get("pin")),
            "bank": None,
            "bit": None,
        }

    bank = str(target.get("bank") or "").strip().upper()
    if bank not in VALID_PUMP_MCP_BANKS:
        bank = base["bank"] if use_legacy_defaults else None

    bit = _coerce_int_or_none(target.get("bit"))
    if bit is None and use_legacy_defaults:
        bit = base["bit"]

    return {
        "controller": controller,
        "pin": None,
        "bank": bank,
        "bit": bit,
    }


def _garden_pump_target_is_complete(target):
    target = _normalize_garden_pump_target(target)
    if target["controller"] == PUMP_CONTROLLER_PI_GPIO:
        return target["pin"] is not None
    return target["bank"] in VALID_PUMP_MCP_BANKS and target["bit"] is not None


def _garden_pump_target_assignment_key(target):
    target = _normalize_garden_pump_target(target)
    if not _garden_pump_target_is_complete(target):
        return None
    if target["controller"] == PUMP_CONTROLLER_PI_GPIO:
        return f"pi_gpio:{target['pin']}"
    return f"mcp23017:{target['bank']}:{target['bit']}"


def _serialize_garden_pump_target(target):
    target = _normalize_garden_pump_target(target, use_legacy_defaults=True)
    return {
        "pump_controller": target["controller"],
        "pump_pi_gpio": target["pin"] if target["controller"] == PUMP_CONTROLLER_PI_GPIO else None,
        "pump_mcp_bank": target["bank"] if target["controller"] == PUMP_CONTROLLER_MCP23017 else None,
        "pump_mcp_bit": target["bit"] if target["controller"] == PUMP_CONTROLLER_MCP23017 else None,
    }


def get_garden_pump_config(garden):
    if not isinstance(garden, dict):
        garden = {}

    raw_days = garden.get("schedule_days_json")
    if isinstance(raw_days, str):
        try:
            raw_days = json.loads(raw_days)
        except Exception:
            raw_days = {}
    if not isinstance(raw_days, dict):
        raw_days = {}

    target = _normalize_garden_pump_target(
        {
            "controller": garden.get("pump_controller"),
            "pin": garden.get("pump_pi_gpio"),
            "bank": garden.get("pump_mcp_bank"),
            "bit": garden.get("pump_mcp_bit"),
        },
        use_legacy_defaults=True,
    )

    return {
        "target": target,
        "assignment_key": _garden_pump_target_assignment_key(target),
        "manual": {
            "enabled": bool(garden.get("pump_manual_enabled", False)),
            "requested_on": bool(garden.get("pump_manual_requested_on", False)),
        },
        "auto_water_enabled": bool(garden.get("auto_water_enabled", False)),
        "interlock_enabled": bool(garden.get("interlock_enabled", True)),
        "schedule": _normalize_schedule(
            {
                "enabled": bool(garden.get("schedule_enabled", False)),
                "start": garden.get("schedule_start", "06:00:00"),
                "stop": garden.get("schedule_stop", "06:15:00"),
                "pump_mode": garden.get("schedule_pump_mode", "on"),
                "autowater_enabled": bool(garden.get("schedule_autowater_enabled", False)),
                "days": raw_days,
            }
        ),
    }


def _normalize_garden_record(garden):
    if not isinstance(garden, dict):
        return garden

    normalized = dict(garden)
    pump_cfg = get_garden_pump_config(normalized)
    target_db = _serialize_garden_pump_target(pump_cfg["target"])
    schedule_cfg = pump_cfg["schedule"]

    normalized.update(target_db)
    normalized["pump_manual_enabled"] = int(pump_cfg["manual"]["enabled"])
    normalized["pump_manual_requested_on"] = int(pump_cfg["manual"]["requested_on"])
    normalized["auto_water_enabled"] = int(pump_cfg["auto_water_enabled"])
    normalized["interlock_enabled"] = int(pump_cfg.get("interlock_enabled", True))
    normalized["schedule_enabled"] = int(schedule_cfg["enabled"])
    normalized["schedule_start"] = schedule_cfg["start"]
    normalized["schedule_stop"] = schedule_cfg["stop"]
    normalized["schedule_pump_mode"] = schedule_cfg["pump_mode"]
    normalized["schedule_autowater_enabled"] = int(schedule_cfg["autowater_enabled"])
    normalized["schedule_days_json"] = json.dumps(schedule_cfg["days"])
    normalized["pump_target"] = pump_cfg["target"]
    normalized["pump_assignment_key"] = pump_cfg["assignment_key"]
    normalized["pump_config"] = pump_cfg
    return normalized


def find_garden_pump_assignment_conflicts(user_id, pump_target, exclude_garden_id=None):
    target_key = _garden_pump_target_assignment_key(pump_target)
    if not target_key:
        return []

    conflicts = []
    for garden in get_all_gardens(user_id):
        if exclude_garden_id is not None and int(garden.get("id", -1)) == int(exclude_garden_id):
            continue
        if garden.get("pump_assignment_key") == target_key:
            conflicts.append(garden)
    return conflicts


def validate_garden_pump_target(target):
    normalized = _normalize_garden_pump_target(target)
    if normalized["controller"] == PUMP_CONTROLLER_PI_GPIO:
        pin = normalized["pin"]
        if pin is None:
            return normalized, "Raspberry Pi GPIO pump assignments require a BCM pin number."
        if not (0 <= pin <= 27):
            return normalized, "Raspberry Pi GPIO pump pin must be between BCM 0 and 27."
        return normalized, None

    bank = normalized["bank"]
    bit = normalized["bit"]
    if bank not in VALID_PUMP_MCP_BANKS:
        return normalized, "MCP23017 pump assignments require bank A or B."
    if bit is None:
        return normalized, "MCP23017 pump assignments require a bit number."
    if not (0 <= bit <= 7):
        return normalized, "MCP23017 pump bit must be between 0 and 7."
    return normalized, None


def update_garden_pump_assignment(garden_id, pump_target):
    target_db = _serialize_garden_pump_target(pump_target)
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE gardens
            SET pump_controller = ?,
                pump_pi_gpio = ?,
                pump_mcp_bank = ?,
                pump_mcp_bit = ?
            WHERE id = ?
            """,
            (
                target_db["pump_controller"],
                target_db["pump_pi_gpio"],
                target_db["pump_mcp_bank"],
                target_db["pump_mcp_bit"],
                garden_id,
            ),
        )


def update_garden_pump_control(garden_id, *, manual=None, schedule=None, auto_water_enabled=None, interlock_enabled=None):
    updates = []
    values = []

    if manual is not None:
        updates.extend([
            "pump_manual_enabled = ?",
            "pump_manual_requested_on = ?",
        ])
        values.extend([
            int(bool(manual.get("enabled", False))),
            int(bool(manual.get("requested_on", False))),
        ])

    if schedule is not None:
        schedule = _normalize_schedule(schedule)
        updates.extend([
            "schedule_enabled = ?",
            "schedule_start = ?",
            "schedule_stop = ?",
            "schedule_pump_mode = ?",
            "schedule_autowater_enabled = ?",
            "schedule_days_json = ?",
        ])
        values.extend([
            int(bool(schedule.get("enabled", False))),
            schedule.get("start", "06:00:00"),
            schedule.get("stop", "06:15:00"),
            schedule.get("pump_mode", "on"),
            int(bool(schedule.get("autowater_enabled", False))),
            json.dumps(schedule.get("days", {})),
        ])

    if auto_water_enabled is not None:
        updates.append("auto_water_enabled = ?")
        values.append(int(bool(auto_water_enabled)))

    if interlock_enabled is not None:
        updates.append("interlock_enabled = ?")
        values.append(int(bool(interlock_enabled)))

    if not updates:
        return

    values.append(garden_id)
    with get_db_connection() as conn:
        conn.execute(
            f"UPDATE gardens SET {', '.join(updates)} WHERE id = ?",
            tuple(values),
        )


def sync_garden_interlock_persistence(garden_id, garden=None, devices=None):
    if garden is None:
        garden = get_garden_by_id(garden_id)
    if not garden:
        return False
    if devices is None:
        devices = get_devices_for_garden(garden_id)

    cfg_snapshot = build_garden_cycle_config(garden, devices)
    has_level_sensor = any(
        info.get("enabled") and is_interlock_sensor_type(info.get("type"))
        for info in cfg_snapshot.get("channels", {}).values()
    )
    stored_enabled = bool((garden.get("pump_config") or {}).get("interlock_enabled", True))

    if stored_enabled and not has_level_sensor:
        update_garden_pump_control(garden_id, interlock_enabled=False)
        return True

    return False


def build_garden_cycle_config(garden, devices=None):
    if not isinstance(garden, dict):
        garden = {}
    if devices is None:
        devices = []

    channels = {ch: {"enabled": False, "type": TYPE_DISABLED} for ch in CHANNELS}

    def add_sensor(sensor_type, channel):
        try:
            ch = int(channel)
        except Exception:
            return
        sensor_type = normalize_sensor_type(sensor_type)
        if ch not in CHANNELS or sensor_type not in VALID_TYPES or sensor_type == TYPE_DISABLED:
            return
        channels[ch] = {"enabled": True, "type": sensor_type}

    add_sensor(garden.get("sensor_type"), garden.get("channel"))
    for device in devices:
        if device.get("node_parent_key"):
            continue
        add_sensor(device.get("sensor_type"), device.get("channel"))

    pump_cfg = garden.get("pump_config") or get_garden_pump_config(garden)
    has_level_sensor = any(info.get("enabled") and is_interlock_sensor_type(info.get("type")) for info in channels.values())
    return {
        "channels": channels,
        "schedule": pump_cfg["schedule"],
        "auto_water_enabled": pump_cfg["auto_water_enabled"],
        "interlock_enabled": bool(pump_cfg.get("interlock_enabled", True)) and has_level_sensor,
        "pump_target": pump_cfg["target"] if isinstance(pump_cfg, dict) else default_garden_pump_target(),
        "manual_override": pump_cfg["manual"] if isinstance(pump_cfg, dict) else {"enabled": False, "requested_on": False},
    }


def get_all_gardens(user_id=None):
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            if user_id:
                cur = conn.execute(
                    "SELECT * FROM gardens WHERE user_id = ? ORDER BY id DESC",
                    (user_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM gardens WHERE user_id IS NULL ORDER BY id DESC"
                )
            return [_normalize_garden_record(dict(row)) for row in cur.fetchall()]
    except Exception:
        return []


def add_garden_record(user_id, name, location, device_type, device_label, ip_address, sensor_type, channel, sensor_name):
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO gardens (
                user_id, name, location, device_type, device_label, ip_address,
                sensor_type, channel, sensor_name, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                name,
                location,
                device_type,
                device_label,
                ip_address,
                sensor_type,
                int(channel),
                sensor_name,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def get_garden_by_id(garden_id: int, user_id=None):
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            if user_id:
                cur = conn.execute(
                    "SELECT * FROM gardens WHERE id = ? AND user_id = ?",
                    (garden_id, user_id),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM gardens WHERE id = ?",
                    (garden_id,),
                )
            row = cur.fetchone()
            return _normalize_garden_record(dict(row)) if row else None
    except Exception:
        return None


def get_devices_for_garden(garden_id: int):
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM devices WHERE garden_id = ? ORDER BY id DESC",
                (garden_id,),
            )
            devices = [dict(row) for row in cur.fetchall()]
        primary_garden = get_garden_by_id(garden_id)
        device_map = {}
        for device in devices:
            try:
                device_id = device.get("id")
                if device_id is not None:
                    device_map[int(device_id)] = device
            except Exception:
                continue
        for device in devices:
            device.update(build_device_display_fields(garden_id, device, device_map=device_map, primary_garden=primary_garden))
        return devices
    except Exception:
        return []


def add_device_record(
    garden_id,
    device_type,
    device_label,
    location,
    ip_address,
    sensor_type,
    channel,
    sensor_name,
    node_parent_key=None,
    gpio_pin=None,
):
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO devices (
                garden_id, device_type, device_label, location, ip_address,
                sensor_type, channel, sensor_name, node_parent_key, gpio_pin, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                garden_id,
                device_type,
                device_label,
                location,
                ip_address,
                sensor_type,
                int(channel),
                sensor_name,
                node_parent_key,
                gpio_pin if gpio_pin is None else int(gpio_pin),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def build_device_display_fields(garden_id, device, device_map=None, primary_garden=None):
    if not isinstance(device, dict):
        return {}

    if device_map is None:
        device_map = {}

    device_type = str(device.get("device_type") or "")
    sensor_type = str(device.get("sensor_type") or "")
    label = DEVICE_TYPE_LABELS.get(
        device_type,
        device_type.replace("_", " ").title(),
    )
    sensor_label = SENSOR_TYPE_LABELS.get(
        sensor_type,
        sensor_type.replace("_", " ").title(),
    )
    channel_label = (
        f"AIN{device.get('channel')}"
        if device.get("sensor_type") != TYPE_DISABLED
        else "Not assigned"
    )

    node_parent_key = (device.get("node_parent_key") or "").strip()
    if node_parent_key:
        parent_type = ""
        if node_parent_key.startswith("garden:") and isinstance(primary_garden, dict):
            parent_type = (primary_garden.get("device_type") or "").strip().lower()
        elif node_parent_key.startswith("device:"):
            try:
                parent_id = int(node_parent_key.split(":", 1)[1])
            except Exception:
                parent_id = None
            parent = device_map.get(parent_id) if parent_id is not None else None
            if isinstance(parent, dict):
                parent_type = (parent.get("device_type") or "").strip().lower()

        if device_type == DEVICE_SENSOR:
            label = "Node Sensor"
        elif device_type == DEVICE_MOTOR:
            label = "Node Motor / Pump"
            sensor_label = "Motor / Pump"

        gpio_pin = device.get("gpio_pin")
        if gpio_pin is not None:
            if parent_type in {DEVICE_PICO, DEVICE_PI}:
                channel_label = _node_sensor_binding_label(
                    parent_type,
                    gpio_pin,
                    sensor_type=device.get("sensor_type"),
                    device_type=device_type,
                )
            else:
                channel_label = f"Node {gpio_pin}"

    return {
        "display_device_type_label": label,
        "display_sensor_type_label": sensor_label,
        "display_channel_label": channel_label,
    }


def build_node_parent_key(node: dict, garden_id=None):
    if not isinstance(node, dict):
        return ""
    source = (node.get("source") or "").strip().lower()
    if source == "primary":
        base_id = garden_id if garden_id is not None else node.get("garden_id") or node.get("id")
        return f"garden:{base_id}" if base_id is not None else ""
    node_id = node.get("id")
    return f"device:{node_id}" if node_id is not None else ""


def get_configured_node_devices(garden_id: int, node_parent_key: str):
    if not node_parent_key:
        return []
    return [
        device
        for device in get_devices_for_garden(garden_id)
        if (device.get("node_parent_key") or "") == node_parent_key
    ]


def get_garden_wifi_nodes(garden_id: int, garden=None, devices=None):
    if garden is None:
        garden = get_garden_by_id(garden_id, session.get("user_id"))
    if devices is None:
        devices = get_devices_for_garden(garden_id)
    nodes = []
    if garden and is_visible_wifi_node(garden):
        primary = dict(garden)
        primary["garden_id"] = garden_id
        primary["source"] = "primary"
        nodes.append(primary)
    for device in devices:
        if is_visible_wifi_node(device):
            node = dict(device)
            node["source"] = "device"
            nodes.append(node)
    return nodes


# ---------------- Pico W WiFi helpers ----------------
def normalize_ip_address(ip_address: str) -> str:
    """Allow plain IPs, hostnames, or full http(s) URLs for Pico W nodes."""
    ip_address = (ip_address or "").strip()
    if not ip_address:
        return ""

    # If the user pasted a full URL, validate the hostname portion but return the URL.
    raw = ip_address
    if raw.startswith("http://") or raw.startswith("https://"):
        without_scheme = raw.split("://", 1)[1]
        host = without_scheme.split("/", 1)[0].split(":", 1)[0]
    else:
        host = raw.split("/", 1)[0].split(":", 1)[0]

    try:
        ipaddress.ip_address(host)
        return raw.rstrip("/")
    except Exception:
        pass

    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"
    if all(c in safe for c in host) and len(host) <= 253:
        return raw.rstrip("/")
    return ""

def pico_base_url(ip_address: str) -> str:
    ip_address = normalize_ip_address(ip_address)
    if not ip_address:
        return ""
    if ip_address.startswith("http://") or ip_address.startswith("https://"):
        return ip_address.rstrip("/")
    return f"http://{ip_address}"

def pico_request(ip_address: str, method: str = "GET", path: str = "/status", payload=None):
    if requests is None:
        return {"online": False, "error": "Python requests package is not installed. Run: pip install requests", "data": None}

    base = pico_base_url(ip_address)
    if not base:
        return {"online": False, "error": "Missing or invalid Pico W IP address", "data": None}

    if not path.startswith("/"):
        path = "/" + path

    url = base + path

    # Pico W MicroPython socket servers are simple. Force short, non-persistent
    # connections and include the API key expected by the Pico firmware.
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connection": "close",
    }
    if PICO_API_KEY:
        headers["X-API-Key"] = PICO_API_KEY

    try:
        method = method.upper()
        if method == "POST":
            r = requests.post(
                url,
                data=json.dumps(payload or {}),
                headers=headers,
                timeout=PICO_HTTP_TIMEOUT_S,
            )
        else:
            r = requests.get(
                url,
                headers=headers,
                timeout=PICO_HTTP_TIMEOUT_S,
            )

        if r.status_code == 401:
            return {
                "online": False,
                "error": "Pico W rejected the request: 401 Unauthorized. Set the same PICO_API_KEY in GardenPi.py and the Pico config.py.",
                "data": None,
            }

        r.raise_for_status()

        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}

        return {
            "online": True,
            "error": None,
            "last_seen": datetime.now().isoformat(timespec="seconds"),
            "data": data,
        }

    except requests.exceptions.ConnectionError as e:
        return {
            "online": False,
            "error": f"Connection error talking to Pico W at {url}: {e}",
            "data": None,
        }
    except requests.exceptions.Timeout:
        return {
            "online": False,
            "error": f"Timeout talking to Pico W at {url}",
            "data": None,
        }
    except Exception as e:
        return {"online": False, "error": str(e), "data": None}

def is_wifi_node_device(device: dict):
    return (device or {}).get("device_type") in WIFI_NODE_DEVICE_TYPES


def is_visible_wifi_node(device: dict):
    return is_wifi_node_device(device) and bool((device or {}).get("ip_address", "").strip())


def pico_status_for_device(device: dict):
    if not is_visible_wifi_node(device):
        return None
    return pico_request(device.get("ip_address", ""), "GET", "/status")


def pico_write_gpio(ip_address: str, pin: int, state: int):
    pin = int(pin)
    state = 1 if int(state) else 0
    if pin not in PICO_VALID_GPIO_PINS:
        return {"online": False, "error": f"GPIO {pin} is not valid for Pico W", "data": None}
    return pico_request(ip_address, "POST", "/gpio/write", {"pin": pin, "state": state})


def get_wifi_devices_for_garden(garden_id: int):
    devices = get_devices_for_garden(garden_id)
    return [d for d in devices if is_visible_wifi_node(d)]


def get_all_wifi_devices(user_id=None):
    wifi_devices = []
    for g in get_all_gardens(user_id):
        if is_visible_wifi_node(g):
            primary = dict(g)
            primary["garden_id"] = g["id"]
            primary["garden_name"] = g["name"]
            primary["source"] = "primary"
            wifi_devices.append(primary)
        for d in get_devices_for_garden(g["id"]):
            if is_visible_wifi_node(d):
                node = dict(d)
                node["garden_id"] = g["id"]
                node["garden_name"] = g["name"]
                node["source"] = "device"
                wifi_devices.append(node)
    return wifi_devices


def build_garden_status_payload(garden, devices=None):
    if devices is None:
        devices = get_devices_for_garden(int(garden.get("id")))

    cfg_snapshot = build_garden_cycle_config(garden, devices)
    stat = compute_cycle_status(cfg_snapshot)
    maybe_log_garden_status(garden, stat)
    enabled_channels = [info for info in stat["channels"].values() if info.get("enabled")]
    moisture_request_count = sum(
        1
        for info in enabled_channels
        if is_moisture_sensor_type(info.get("type"))
        and (info.get("reading") or {}).get("pump_request")
    )
    refill_alert_count = sum(
        1
        for info in enabled_channels
        if is_interlock_sensor_type(info.get("type"))
        and (info.get("reading") or {}).get("state") == "NEEDS_REFILL"
    )

    return {
        "garden": garden,
        "result": stat,
        "summary": {
            "enabled_channel_count": len(enabled_channels),
            "moisture_request_count": moisture_request_count,
            "refill_alert_count": refill_alert_count,
            "attention_needed": bool(stat["interlock"]["active"] or moisture_request_count or refill_alert_count),
        },
    }


def _normalize_history_range(history_range):
    history_range = str(history_range or DEFAULT_HISTORY_RANGE).strip().lower()
    if history_range not in VALID_HISTORY_RANGES:
        return DEFAULT_HISTORY_RANGE
    return history_range


def _history_cutoff_iso(history_range):
    history_range = _normalize_history_range(history_range)
    hours = VALID_HISTORY_RANGES[history_range]["hours"]
    if hours is None:
        return None
    return (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")


def build_garden_history_payload(garden, history_range=DEFAULT_HISTORY_RANGE, point_limit=MAX_HISTORY_POINTS):
    if not isinstance(garden, dict) or not garden.get("id"):
        return {
            "range": _normalize_history_range(history_range),
            "range_options": [],
            "channels": {},
            "pump_events": [],
            "point_limit": 0,
            "summary": {
                "reading_count": 0,
                "pump_event_count": 0,
                "channel_count": 0,
            },
        }

    history_range = _normalize_history_range(history_range)
    try:
        point_limit = int(point_limit)
    except Exception:
        point_limit = MAX_HISTORY_POINTS
    point_limit = max(50, min(point_limit, MAX_HISTORY_POINTS))
    cutoff_iso = _history_cutoff_iso(history_range)

    sensor_sql = """
        SELECT * FROM (
            SELECT sr.id, sr.channel, sr.sensor_type, sr.voltage, sr.wet_percent,
                   sr.state, sr.label, sr.pump_request, sr.created_at
            FROM sensor_readings sr
            WHERE sr.garden_id = ?
    """
    sensor_params = [int(garden["id"])]
    if cutoff_iso:
        sensor_sql += " AND datetime(sr.created_at) >= datetime(?)"
        sensor_params.append(cutoff_iso)
    sensor_sql += " ORDER BY datetime(sr.created_at) DESC, sr.id DESC LIMIT ? ) ORDER BY datetime(created_at) ASC, id ASC"
    sensor_params.append(point_limit)

    pump_sql = """
        SELECT * FROM (
            SELECT pe.id, pe.pump_state, pe.pump_reason, pe.pump_target_json,
                   pe.output_active, pe.created_at
            FROM pump_events pe
            WHERE pe.garden_id = ?
    """
    pump_params = [int(garden["id"])]
    if cutoff_iso:
        pump_sql += " AND datetime(pe.created_at) >= datetime(?)"
        pump_params.append(cutoff_iso)
    pump_sql += " ORDER BY datetime(pe.created_at) DESC, pe.id DESC LIMIT ? ) ORDER BY datetime(created_at) ASC, id ASC"
    pump_params.append(point_limit)

    channels = {}
    pump_events = []
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        sensor_rows = conn.execute(sensor_sql, sensor_params).fetchall()
        pump_rows = conn.execute(pump_sql, pump_params).fetchall()

    for row in sensor_rows:
        channel = int(row["channel"])
        channel_key = str(channel)
        sensor_type = row["sensor_type"] or TYPE_DISABLED
        item = channels.setdefault(
            channel_key,
            {
                "channel": channel,
                "channel_label": f"AIN{channel}",
                "sensor_type": sensor_type,
                "sensor_type_label": SENSOR_TYPE_LABELS.get(sensor_type, sensor_type.replace("_", " ").title()),
                "points": [],
            },
        )
        item["points"].append(
            {
                "id": row["id"],
                "timestamp": row["created_at"],
                "voltage": row["voltage"],
                "wet_percent": row["wet_percent"],
                "state": row["state"],
                "label": row["label"],
                "pump_request": int(bool(row["pump_request"])),
            }
        )

    for row in pump_rows:
        try:
            pump_target = json.loads(row["pump_target_json"] or "{}")
        except Exception:
            pump_target = {}
        pump_events.append(
            {
                "id": row["id"],
                "timestamp": row["created_at"],
                "pump_state": row["pump_state"],
                "pump_reason": row["pump_reason"],
                "pump_target": pump_target,
                "output_active": int(bool(row["output_active"])),
            }
        )

    return {
        "range": history_range,
        "range_label": VALID_HISTORY_RANGES[history_range]["label"],
        "range_options": [
            {"key": key, "label": meta["label"]}
            for key, meta in VALID_HISTORY_RANGES.items()
        ],
        "point_limit": point_limit,
        "channels": channels,
        "pump_events": pump_events,
        "summary": {
            "reading_count": len(sensor_rows),
            "pump_event_count": len(pump_events),
            "channel_count": len(channels),
        },
    }


def get_dashboard_garden_statuses(user_id=None):
    statuses = []
    for garden in get_all_gardens(user_id):
        devices = get_devices_for_garden(garden["id"])
        payload = build_garden_status_payload(garden, devices)
        statuses.append(payload)
    return statuses


def build_dashboard_summary(garden_statuses):
    return {
        "active_gardens": len(garden_statuses),
        "watering_now": sum(1 for item in garden_statuses if item["result"].get("pump_state") == "ON"),
        "attention_needed": sum(1 for item in garden_statuses if item["summary"].get("attention_needed")),
        "armed_schedules": sum(1 for item in garden_statuses if item["result"].get("schedule", {}).get("enabled")),
        "interlock_enabled_gardens": sum(
            1 for item in garden_statuses if (item["result"].get("interlock") or {}).get("enabled")
        ),
        "interlock_active_gardens": sum(
            1 for item in garden_statuses if (item["result"].get("interlock") or {}).get("active")
        ),
        "interlock_blocked_gardens": sum(1 for item in garden_statuses if item["result"].get("pump_reason") == "INTERLOCK"),
    }

# ---------------- Schedule helpers ----------------
def _parse_hhmmss(txt):
    try:
        parts = txt.split(":")
        if len(parts) == 2:
            hh, mm = parts
            ss = 0
        elif len(parts) == 3:
            hh, mm, ss = parts
        else:
            return None

        hh = int(hh)
        mm = int(mm)
        ss = int(ss)

        if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
            return hh, mm, ss
    except Exception:
        return None
    return None


def is_schedule_active(schedule_cfg):
    if not schedule_cfg.get("enabled", False):
        return False

    now = datetime.now()
    weekday_key = DAY_KEYS[now.weekday()]
    days = schedule_cfg.get("days", {})

    if not days.get(weekday_key, False):
        return False

    start = _parse_hhmmss(schedule_cfg.get("start", "06:00:00"))
    stop = _parse_hhmmss(schedule_cfg.get("stop", "06:15:00"))

    if not start or not stop:
        return False

    now_seconds = now.hour * 3600 + now.minute * 60 + now.second
    start_seconds = start[0] * 3600 + start[1] * 60 + start[2]
    stop_seconds = stop[0] * 3600 + stop[1] * 60 + stop[2]

    if start_seconds <= stop_seconds:
        return start_seconds <= now_seconds < stop_seconds
    return now_seconds >= start_seconds or now_seconds < stop_seconds


# ---------------- Sensor math ----------------
def soil_wetness(v: float) -> float:
    if v >= SOIL_WET_V:
        return 100.0
    if v <= SOIL_DRY_V:
        return 0.0
    pct = 100.0 * (v - SOIL_DRY_V) / (SOIL_WET_V - SOIL_DRY_V)
    return max(0.0, min(100.0, pct))


def top_hat_soil_scale(sensor_type=TYPE_MOISTURE_RESISTIVE):
    profile = moisture_sensor_profile(sensor_type)
    wet_v = max(0.0, top_hat_output_voltage(profile["wet_v"]))
    dry_v = max(0.0, top_hat_output_voltage(profile["dry_v"]))
    pump_on_v = top_hat_output_voltage(profile["pump_on_v"])
    lower_bound = min(wet_v, dry_v)
    upper_bound = max(wet_v, dry_v)
    pump_on_v = max(lower_bound, min(upper_bound, pump_on_v))
    higher_is_wetter = wet_v >= dry_v
    return {
        "sensor_type": profile["sensor_type"],
        "label": profile["label"],
        "wet_v": wet_v,
        "dry_v": dry_v,
        "pump_on_v": pump_on_v,
        "higher_is_wetter": higher_is_wetter,
    }


def top_hat_soil_wetness(display_v: float, sensor_type=TYPE_MOISTURE_RESISTIVE) -> float:
    scale = top_hat_soil_scale(sensor_type)
    wet_v = scale["wet_v"]
    dry_v = scale["dry_v"]
    if wet_v == dry_v:
        return 0.0
    if scale["higher_is_wetter"]:
        if display_v >= wet_v:
            return 100.0
        if display_v <= dry_v:
            return 0.0
        pct = 100.0 * (display_v - dry_v) / (wet_v - dry_v)
    else:
        if display_v <= wet_v:
            return 100.0
        if display_v >= dry_v:
            return 0.0
        pct = 100.0 * (dry_v - display_v) / (dry_v - wet_v)
    return max(0.0, min(100.0, pct))


def top_hat_soil_wants_pump(display_v: float, sensor_type=TYPE_MOISTURE_RESISTIVE) -> bool:
    scale = top_hat_soil_scale(sensor_type)
    if scale["higher_is_wetter"]:
        return display_v < scale["pump_on_v"]
    return display_v > scale["pump_on_v"]


def ir_level_state(v: float):
    if v <= IR_WATER_PRESENT_MAX_V:
        return ("WATER_PRESENT", "Water present", "success")
    if v >= IR_NEEDS_REFILL_MIN_V:
        return ("NEEDS_REFILL", "Needs refill", "danger")
    return ("BORDERLINE", "Borderline", "warning")


def compute_cycle_status(cfg_snapshot: dict):
    channels_cfg = cfg_snapshot["channels"]
    schedule_cfg = _normalize_schedule(cfg_snapshot["schedule"])
    auto_water_enabled = bool(cfg_snapshot.get("auto_water_enabled", False))
    interlock_enabled = bool(cfg_snapshot.get("interlock_enabled", True))
    pump_target = _normalize_garden_pump_target(
        cfg_snapshot.get("pump_target"),
        use_legacy_defaults=True,
    )
    manual = _normalize_manual_override(cfg_snapshot.get("manual_override"))

    per_channel = {}
    any_moisture_wants_pump = False
    interlock_active = False
    interlock_sources = []

    for ch in CHANNELS:
        ch_cfg = channels_cfg.get(ch, {"enabled": False, "type": TYPE_DISABLED})
        enabled = bool(ch_cfg.get("enabled", False))
        typ = ch_cfg.get("type", TYPE_DISABLED)

        if not enabled or typ == TYPE_DISABLED:
            per_channel[ch] = {
                "enabled": False,
                "type": TYPE_DISABLED,
                "reading": None,
            }
            continue

        raw_v = read_voltage(ch)
        output_v = top_hat_output_voltage(raw_v)

        if is_moisture_sensor_type(typ):
            typ = normalize_sensor_type(typ)
            soil_scale = top_hat_soil_scale(typ)
            wet = top_hat_soil_wetness(output_v, typ)
            wants_pump = top_hat_soil_wants_pump(output_v, typ)
            if wants_pump:
                any_moisture_wants_pump = True

            per_channel[ch] = {
                "enabled": True,
                "type": typ,
                "reading": {
                    "voltage": round(output_v, 3),
                    "raw_voltage": round(raw_v, 3),
                    "wet_percent": wet,
                    "scale": {
                        "wet_v": round(soil_scale["wet_v"], 3),
                        "dry_v": round(soil_scale["dry_v"], 3),
                    },
                    "rule": f"Pump ON if V < {soil_scale['pump_on_v']:.3f} V",
                    "pump_request": wants_pump,
                },
            }

        elif typ == TYPE_LEVEL:
            state, label, css = ir_level_state(raw_v)
            if state == "NEEDS_REFILL":
                interlock_active = True
                interlock_sources.append({"channel": ch, "voltage": round(output_v, 3), "raw_voltage": round(raw_v, 3)})

            per_channel[ch] = {
                "enabled": True,
                "type": TYPE_LEVEL,
                "reading": {
                    "voltage": round(output_v, 3),
                    "raw_voltage": round(raw_v, 3),
                    "state": state,
                    "label": label,
                    "css": css,
                    "thresholds": {
                        "present_max_v": round(top_hat_output_voltage(IR_WATER_PRESENT_MAX_V), 3),
                        "refill_min_v": round(top_hat_output_voltage(IR_NEEDS_REFILL_MIN_V), 3),
                    },
                },
            }

        elif typ == TYPE_WATER_LEVEL:
            per_channel[ch] = {
                "enabled": True,
                "type": TYPE_WATER_LEVEL,
                "reading": {
                    "voltage": round(output_v, 3),
                    "raw_voltage": round(raw_v, 3),
                    "state": "ANALOG",
                    "label": "Water level sensor connected",
                },
            }

        elif typ == TYPE_LIQUID:
            per_channel[ch] = {
                "enabled": True,
                "type": TYPE_LIQUID,
                "reading": {
                    "voltage": round(output_v, 3),
                    "raw_voltage": round(raw_v, 3),
                    "state": "ANALOG",
                    "label": "Liquid sensor connected",
                },
            }

        elif typ == TYPE_TEMPERATURE:
            per_channel[ch] = {
                "enabled": True,
                "type": TYPE_TEMPERATURE,
                "reading": {
                    "voltage": round(output_v, 3),
                    "raw_voltage": round(raw_v, 3),
                    "state": "ANALOG",
                    "label": "Temperature sensor connected",
                },
            }

        elif typ == TYPE_DHT11:
            per_channel[ch] = {
                "enabled": True,
                "type": TYPE_DHT11,
                "reading": {
                    "voltage": round(output_v, 3),
                    "raw_voltage": round(raw_v, 3),
                    "state": "CONNECTED",
                    "label": "DHT11 sensor connected",
                },
            }

    schedule_active = is_schedule_active(schedule_cfg)

    schedule_direct_request_on = (
        schedule_cfg.get("enabled", False)
        and schedule_active
        and schedule_cfg.get("pump_mode", "on") == "on"
    )
    schedule_direct_request_off = (
        schedule_cfg.get("enabled", False)
        and schedule_active
        and schedule_cfg.get("pump_mode", "on") == "off"
    )
    schedule_autowater_request = (
        schedule_cfg.get("enabled", False)
        and schedule_active
        and schedule_cfg.get("autowater_enabled", False)
        and any_moisture_wants_pump
    )
    base_autowater_request = auto_water_enabled and any_moisture_wants_pump

    if interlock_enabled and interlock_active:
        clear_pump(target=pump_target)
        pump_state = "OFF"
        pump_reason = "INTERLOCK"
    elif manual["enabled"]:
        set_pump(manual["requested_on"], target=pump_target)
        pump_state = "ON" if manual["requested_on"] else "OFF"
        pump_reason = "MANUAL"
    elif schedule_direct_request_on:
        set_pump(True, target=pump_target)
        pump_state = "ON"
        pump_reason = "SCHEDULE"
    elif schedule_direct_request_off:
        set_pump(False, target=pump_target)
        pump_state = "OFF"
        pump_reason = "SCHEDULE"
    elif schedule_autowater_request:
        set_pump(True, target=pump_target)
        pump_state = "ON"
        pump_reason = "SCHEDULE_AUTO"
    elif base_autowater_request:
        set_pump(True, target=pump_target)
        pump_state = "ON"
        pump_reason = "AUTO"
    else:
        set_pump(False, target=pump_target)
        pump_state = "OFF"
        pump_reason = "IDLE"

    return {
        "channels": per_channel,
        "pump_state": pump_state,
        "pump_reason": pump_reason,
        "pump_target": pump_target,
        "pump_output_active": get_pump_output_state(pump_target),
        "manual_override": manual,
        "schedule": {
            "enabled": schedule_cfg.get("enabled", False),
            "active_now": schedule_active,
            "start": schedule_cfg.get("start", "06:00:00"),
            "stop": schedule_cfg.get("stop", "06:15:00"),
            "pump_mode": schedule_cfg.get("pump_mode", "on"),
            "autowater_enabled": schedule_cfg.get("autowater_enabled", False),
            "days": schedule_cfg.get("days", {}),
        },
        "auto_water_enabled": auto_water_enabled,
        "interlock": {
            "enabled": interlock_enabled,
            "active": interlock_active,
            "sources": interlock_sources,
        },
    }


# ---------------- Template context ----------------
def common_template_context():
    return {
        "logged_in": bool(session.get("user_id")),
        "username": session.get("username"),
    }


def build_system_page_context(user_id=None):
    info = {
        "base_dir": BASE_DIR,
        "db_path": DB_PATH,
        "db_exists": os.path.exists(DB_PATH),
        "db_size_bytes": None,
        "db_last_modified": None,
        "mock": MOCK,
        "hw_errors": hw_errors[-10:],
        "health_ok": True,
        "health_issues": [],
        "env": {
            "FLASK_SECRET_KEY_set": bool(os.environ.get("FLASK_SECRET_KEY")),
            "GARDENPI_MOCK": os.environ.get("GARDENPI_MOCK"),
        },
        "db_tables": [],
        "record_counts": {},
        "log_tables": [],
        "recent_gardens": [],
        "recent_devices": [],
        "recent_sensor_readings": [],
        "recent_pump_events": [],
    }

    if info["db_exists"]:
        try:
            info["db_size_bytes"] = os.path.getsize(DB_PATH)
            info["db_last_modified"] = datetime.fromtimestamp(os.path.getmtime(DB_PATH)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            info["health_ok"] = False
            info["health_issues"].append(f"DB file metadata error: {e}")

    required_templates = [
        "base.html",
        "login.html",
        "register.html",
        "dashboard.html",
        "garden.html",
        "garden_detail.html",
        "schedule.html",
        "settings.html",
        "help.html",
    ]
    for template_name in required_templates:
        if not os.path.exists(os.path.join(BASE_DIR, "templates", template_name)):
            info["health_ok"] = False
            info["health_issues"].append(f"Missing template: {template_name}")

    if MOCK:
        info["health_issues"].append("MOCK hardware enabled")
    if hw_errors:
        info["health_issues"].extend(hw_errors[-5:])

    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE IF NOT EXISTS _health (x INTEGER)")
            table_rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = [row[0] for row in table_rows if row[0] != "sqlite_sequence"]
            info["db_tables"] = table_names

            for table_name in ["users", "settings", "gardens", "devices", "sensor_readings", "pump_events"]:
                if table_name in table_names:
                    info["record_counts"][table_name] = conn.execute(
                        f"SELECT COUNT(*) FROM {table_name}"
                    ).fetchone()[0]

            info["log_tables"] = [
                name for name in table_names
                if any(token in name.lower() for token in ("log", "logs", "reading", "readings", "history", "event", "events"))
            ]

            recent_garden_sql = """
                SELECT id, name, location, created_at
                FROM gardens
            """
            recent_garden_params = []
            if user_id:
                recent_garden_sql += " WHERE user_id = ?"
                recent_garden_params.append(user_id)
            recent_garden_sql += " ORDER BY datetime(created_at) DESC, id DESC LIMIT 5"
            if "gardens" in table_names:
                info["recent_gardens"] = [
                    dict(row) for row in conn.execute(recent_garden_sql, recent_garden_params).fetchall()
                ]

            recent_device_sql = """
                SELECT d.id, d.device_type, d.device_label, d.sensor_type, d.channel,
                       d.node_parent_key, d.gpio_pin, d.created_at,
                       g.id AS garden_id, g.name AS garden_name, g.device_type AS primary_device_type,
                       pd.id AS parent_device_id, pd.device_type AS parent_device_type
                FROM devices d
                JOIN gardens g ON g.id = d.garden_id
                LEFT JOIN devices pd ON d.node_parent_key = ('device:' || pd.id)
            """
            recent_device_params = []
            if user_id:
                recent_device_sql += " WHERE g.user_id = ?"
                recent_device_params.append(user_id)
            recent_device_sql += " ORDER BY datetime(d.created_at) DESC, d.id DESC LIMIT 5"
            if "devices" in table_names and "gardens" in table_names:
                recent_devices = [
                    dict(row) for row in conn.execute(recent_device_sql, recent_device_params).fetchall()
                ]
                for device in recent_devices:
                    parent_device_type = (device.get("parent_device_type") or "").strip()
                    parent_device_id = device.get("parent_device_id")
                    parent_map = (
                        {int(parent_device_id): {"device_type": parent_device_type}}
                        if parent_device_id is not None and parent_device_type
                        else {}
                    )
                    primary_garden = {"device_type": device.get("primary_device_type")}
                    device.update(
                        build_device_display_fields(
                            device.get("garden_id"),
                            device,
                            device_map=parent_map,
                            primary_garden=primary_garden,
                        )
                    )
                    sensor_type = str(device.get("sensor_type") or "")
                    device["display_sensor_type_label"] = SENSOR_TYPE_LABELS.get(
                        sensor_type,
                        sensor_type.replace("_", " ").title() or "Not assigned",
                    )
                info["recent_devices"] = recent_devices

            recent_sensor_sql = """
                SELECT sr.id, sr.channel, sr.sensor_type, sr.voltage, sr.wet_percent,
                       sr.state, sr.label, sr.pump_request, sr.created_at,
                       g.id AS garden_id, g.name AS garden_name
                FROM sensor_readings sr
                JOIN gardens g ON g.id = sr.garden_id
            """
            recent_sensor_params = []
            if user_id:
                recent_sensor_sql += " WHERE g.user_id = ?"
                recent_sensor_params.append(user_id)
            recent_sensor_sql += " ORDER BY datetime(sr.created_at) DESC, sr.id DESC LIMIT 10"
            if "sensor_readings" in table_names and "gardens" in table_names:
                info["recent_sensor_readings"] = [
                    dict(row) for row in conn.execute(recent_sensor_sql, recent_sensor_params).fetchall()
                ]

            recent_pump_sql = """
                SELECT pe.id, pe.pump_state, pe.pump_reason, pe.pump_target_json,
                       pe.output_active, pe.created_at,
                       g.id AS garden_id, g.name AS garden_name
                FROM pump_events pe
                JOIN gardens g ON g.id = pe.garden_id
            """
            recent_pump_params = []
            if user_id:
                recent_pump_sql += " WHERE g.user_id = ?"
                recent_pump_params.append(user_id)
            recent_pump_sql += " ORDER BY datetime(pe.created_at) DESC, pe.id DESC LIMIT 10"
            if "pump_events" in table_names and "gardens" in table_names:
                info["recent_pump_events"] = [
                    dict(row) for row in conn.execute(recent_pump_sql, recent_pump_params).fetchall()
                ]
    except Exception as e:
        info["health_ok"] = False
        info["health_issues"].append(f"DB problem: {e}")

    return info


# ---------------- Auth routes ----------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if not username or not password:
            flash("Username and password are required.", "danger")
            return redirect(url_for("register"))

        if create_user(username, password):
            user = get_user(username)
            if user:
                save_settings(user["id"], default_config())
            flash("Registration successful. Please log in.", "success")
            return redirect(url_for("login"))

        flash("Username is already taken or DB error.", "danger")

    return render_template("register.html", **common_template_context())


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = get_user(username)
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid username or password.", "danger")
            return redirect(url_for("login"))

        session["user_id"] = user["id"]
        session["username"] = user["username"]
        flash(f"Welcome, {user['username']}!", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html", **common_template_context())


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ---------------- Page routes ----------------
@app.route("/")
def index():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html", **common_template_context())


@app.route("/garden")
def garden():
    gardens = get_all_gardens(session.get("user_id"))
    return render_template(
        "garden.html",
        gardens=gardens,
        channels=CHANNELS,
        device_type_labels=DEVICE_TYPE_LABELS,
        sensor_type_labels=SENSOR_TYPE_LABELS,
        **common_template_context(),
    )


@app.route("/garden/<int:garden_id>")
def garden_detail(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        flash("Garden not found.", "danger")
        return redirect(url_for("garden"))

    devices = get_devices_for_garden(garden_id)
    garden_cfg = build_garden_cycle_config(garden, devices)
    wifi_nodes = get_garden_wifi_nodes(garden_id, garden=garden, devices=devices)
    return render_template(
        "garden_detail.html",
        garden=garden,
        devices=devices,
        has_wifi_nodes=bool(wifi_nodes),
        channels=CHANNELS,
        schedule_cfg=garden_cfg["schedule"],
        auto_water_enabled=garden_cfg["auto_water_enabled"],
        manual_override=garden_cfg["manual_override"],
        day_keys=DAY_KEYS,
        device_type_labels=DEVICE_TYPE_LABELS,
        sensor_type_labels=SENSOR_TYPE_LABELS,
        pump_controllers={
            "pi_gpio": PUMP_CONTROLLER_PI_GPIO,
            "mcp23017": PUMP_CONTROLLER_MCP23017,
        },
        mcp_banks=sorted(VALID_PUMP_MCP_BANKS),
        **common_template_context(),
    )


@app.route("/schedule")
def schedule_page():
    gardens = get_all_gardens(session.get("user_id"))
    garden_schedules = []
    for garden in gardens:
        pump_cfg = garden.get("pump_config") or {}
        garden_schedules.append(
            {
                "garden": garden,
                "schedule_cfg": pump_cfg.get("schedule", _normalize_schedule({})),
                "auto_water_enabled": bool(pump_cfg.get("auto_water_enabled", False)),
                "manual_override": pump_cfg.get("manual", {"enabled": False, "requested_on": False}),
            }
        )
    return render_template(
        "schedule.html",
        garden_schedules=garden_schedules,
        day_keys=DAY_KEYS,
        **common_template_context(),
    )


@app.route("/settings")
def settings_page():
    cfg_snapshot = build_legacy_cycle_config()
    status = compute_cycle_status(cfg_snapshot)
    system_info = build_system_page_context(session.get("user_id"))
    return render_template(
        "settings.html",
        cfg=cfg_snapshot,
        status=status,
        system_info=system_info,
        soil_range={
            "wet": round(top_hat_soil_scale(TYPE_MOISTURE_RESISTIVE)["wet_v"], 3),
            "dry": round(top_hat_soil_scale(TYPE_MOISTURE_RESISTIVE)["dry_v"], 3),
        },
        pump_rule=round(top_hat_soil_scale(TYPE_MOISTURE_RESISTIVE)["pump_on_v"], 3),
        ir_thresholds={
            "present_max_v": round(top_hat_output_voltage(IR_WATER_PRESENT_MAX_V), 3),
            "refill_min_v": round(top_hat_output_voltage(IR_NEEDS_REFILL_MIN_V), 3),
        },
        mock=MOCK,
        hw_errors=hw_errors[-10:],
        **common_template_context(),
    )


@app.route("/help")
def help_page():
    return render_template("help.html", **common_template_context())


# ---------------- Garden actions ----------------
@app.route("/garden/add", methods=["POST"])
def garden_add():
    name = (request.form.get("name") or "").strip()
    location = (request.form.get("location") or "").strip().lower()

    if not name:
        flash("Garden name is required.", "danger")
        return redirect(url_for("garden"))
    if location not in VALID_LOCATIONS:
        flash("Invalid location.", "danger")
        return redirect(url_for("garden"))

    try:
        add_garden_record(
            session.get("user_id"),
            name,
            location,
            DEVICE_UNASSIGNED,
            "",
            "",
            TYPE_DISABLED,
            CHANNELS[0],
            "",
        )
        gardens = get_all_gardens(session.get("user_id"))
        created_garden = gardens[0] if gardens else None
        created_garden_id = created_garden.get("id") if created_garden else None
        if created_garden_id is not None:
            sync_garden_interlock_persistence(
                created_garden_id,
                created_garden,
                get_devices_for_garden(int(created_garden_id)),
            )
        flash("Garden added.", "success")
    except Exception as e:
        flash(f"Failed to add garden: {e}", "danger")

    return redirect(url_for("garden"))


@app.route("/garden/<int:garden_id>/delete", methods=["POST"])
def delete_garden(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        flash("Garden not found.", "danger")
        return redirect(url_for("garden"))

    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM gardens WHERE id = ?", (garden_id,))
        flash("Garden deleted.", "success")
    except Exception as e:
        flash(f"Failed to delete garden: {e}", "danger")

    return redirect(url_for("garden"))


@app.route("/garden/<int:garden_id>/pump/save", methods=["POST"])
def save_garden_pump_assignment(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        flash("Garden not found.", "danger")
        return redirect(url_for("garden"))

    controller = str(request.form.get("pump_controller", PUMP_CONTROLLER_MCP23017)).strip().lower()
    raw_target = {"controller": controller}
    if controller == PUMP_CONTROLLER_PI_GPIO:
        raw_target["pin"] = request.form.get("pump_pi_gpio")
    else:
        raw_target["bank"] = request.form.get("pump_mcp_bank")
        raw_target["bit"] = request.form.get("pump_mcp_bit")

    normalized_target, error = validate_garden_pump_target(raw_target)
    if error:
        flash(error, "danger")
        return redirect(url_for("garden_detail", garden_id=garden_id))

    conflicts = find_garden_pump_assignment_conflicts(
        session.get("user_id"),
        normalized_target,
        exclude_garden_id=garden_id,
    )
    if conflicts:
        conflict_names = ", ".join(g.get("name") or f"Garden {g.get('id')}" for g in conflicts)
        flash(f"Pump output already assigned to: {conflict_names}", "danger")
        return redirect(url_for("garden_detail", garden_id=garden_id))

    try:
        update_garden_pump_assignment(garden_id, normalized_target)
        flash("Garden pump assignment saved.", "success")
    except Exception as e:
        flash(f"Failed to save garden pump assignment: {e}", "danger")

    return redirect(url_for("garden_detail", garden_id=garden_id))


@app.route("/garden/<int:garden_id>/device/add", methods=["POST"])
def add_garden_device(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        flash("Garden not found.", "danger")
        return redirect(url_for("garden"))

    device_kind = (request.form.get("device_kind") or "").strip().lower()
    location = garden.get("location") if garden.get("location") in VALID_LOCATIONS else "indoor"

    if device_kind == "sensor":
        device_type = DEVICE_SENSOR
        device_label = ""
        ip_address = ""
        sensor_type = normalize_sensor_type((request.form.get("sensor_type") or "").strip().lower())
        channel_raw = request.form.get("channel", "0")
        sensor_name = (request.form.get("sensor_name") or "").strip()

        try:
            channel = int(channel_raw)
        except Exception:
            channel = -1

        if sensor_type not in VALID_TYPES or sensor_type == TYPE_DISABLED:
            flash("Invalid sensor type.", "danger")
            return redirect(url_for("garden_detail", garden_id=garden_id))
        if channel not in CHANNELS:
            flash("Invalid channel.", "danger")
            return redirect(url_for("garden_detail", garden_id=garden_id))
        if not sensor_name:
            sensor_name = SENSOR_TYPE_LABELS.get(sensor_type, "")

    elif device_kind == "node":
        device_type = (request.form.get("node_type") or "").strip().lower()
        device_label = ""
        ip_address = normalize_ip_address((request.form.get("ip_address") or "").strip())
        sensor_type = TYPE_DISABLED
        channel = CHANNELS[0]
        sensor_name = ""

        if device_type not in {DEVICE_PI, DEVICE_PICO}:
            flash("Invalid node type.", "danger")
            return redirect(url_for("garden_detail", garden_id=garden_id))
        if not ip_address:
            flash("Node devices need an IP address or hostname.", "danger")
            return redirect(url_for("garden_detail", garden_id=garden_id))
    else:
        flash("Choose whether you are adding a sensor or a node.", "danger")
        return redirect(url_for("garden_detail", garden_id=garden_id))

    try:
        add_device_record(
            garden_id,
            device_type,
            device_label,
            location,
            ip_address,
            sensor_type,
            channel,
            sensor_name,
        )
        sync_garden_interlock_persistence(garden_id, garden, get_devices_for_garden(garden_id))
        flash("Device added.", "success")
    except Exception as e:
        flash(f"Failed to add device: {e}", "danger")

    return redirect(url_for("garden_detail", garden_id=garden_id))


@app.route("/garden/<int:garden_id>/node-device/add", methods=["POST"])
def add_garden_node_device(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        flash("Garden not found.", "danger")
        return redirect(url_for("garden"))

    devices = get_devices_for_garden(garden_id)
    nodes = get_garden_wifi_nodes(garden_id, garden=garden, devices=devices)
    nodes_by_key = {build_node_parent_key(node, garden_id): node for node in nodes}

    node_parent_key = (request.form.get("node_parent_key") or "").strip()
    node = nodes_by_key.get(node_parent_key)
    if not node:
        flash("Choose a valid node first.", "danger")
        return redirect(url_for("garden_detail", garden_id=garden_id))

    node_type = (node.get("device_type") or "").strip().lower()
    device_kind = (request.form.get("device_kind") or "").strip().lower()
    sensor_name = (request.form.get("sensor_name") or "").strip()
    motor_name = (request.form.get("motor_name") or "").strip()
    location = node.get("location") if node.get("location") in VALID_LOCATIONS else (garden.get("location") if garden.get("location") in VALID_LOCATIONS else LOCATION_INDOOR)

    if device_kind == "sensor":
        sensor_type = normalize_sensor_type((request.form.get("sensor_type") or "").strip().lower())
        gpio_raw = request.form.get("gpio_pin", "")
        try:
            gpio_pin = int(gpio_raw)
        except Exception:
            gpio_pin = None

        if sensor_type not in VALID_TYPES or sensor_type == TYPE_DISABLED:
            flash("Invalid sensor type.", "danger")
            return redirect(url_for("garden_detail", garden_id=garden_id))
        if gpio_pin is None:
            flash("Choose which GPIO/ADC input the sensor is using.", "danger")
            return redirect(url_for("garden_detail", garden_id=garden_id))

        if sensor_type_requires_analog_input(sensor_type):
            if node_type == DEVICE_PICO and gpio_pin not in PICO_SENSOR_PINS:
                flash("Pico analog sensors must use GP26, GP27, or GP28.", "danger")
                return redirect(url_for("garden_detail", garden_id=garden_id))
            if node_type == DEVICE_PI and gpio_pin not in RPI_TOP_HAT_ADC_CHANNELS:
                flash("Top HAT analog sensors must use AIN0, AIN1, or AIN2.", "danger")
                return redirect(url_for("garden_detail", garden_id=garden_id))
        elif sensor_type_requires_gpio_input(sensor_type):
            if node_type == DEVICE_PICO and gpio_pin not in PICO_VALID_GPIO_PINS:
                flash("Choose a valid Pico GPIO pin for this sensor.", "danger")
                return redirect(url_for("garden_detail", garden_id=garden_id))
            if node_type == DEVICE_PI and gpio_pin < 0:
                flash("Choose a valid GPIO pin for this sensor.", "danger")
                return redirect(url_for("garden_detail", garden_id=garden_id))

        add_device_record(
            garden_id,
            DEVICE_SENSOR,
            "",
            location,
            "",
            sensor_type,
            0,
            sensor_name or SENSOR_TYPE_LABELS.get(sensor_type, "Node Sensor"),
            node_parent_key=node_parent_key,
            gpio_pin=gpio_pin,
        )
        flash("Node sensor added.", "success")
        return redirect(url_for("garden_detail", garden_id=garden_id))

    if device_kind == "motor":
        gpio_raw = request.form.get("motor_gpio_pin", "")
        try:
            gpio_pin = int(gpio_raw)
        except Exception:
            gpio_pin = None

        if gpio_pin is None:
            flash("Enter the GPIO pin used for the motor / pump on/off control.", "danger")
            return redirect(url_for("garden_detail", garden_id=garden_id))
        if node_type == DEVICE_PICO and gpio_pin not in PICO_OUTPUT_PINS:
            flash("Pico motor / pump control pins must use one of the configured output GPIOs.", "danger")
            return redirect(url_for("garden_detail", garden_id=garden_id))
        if node_type == DEVICE_PI and gpio_pin < 0:
            flash("Invalid GPIO pin.", "danger")
            return redirect(url_for("garden_detail", garden_id=garden_id))

        add_device_record(
            garden_id,
            DEVICE_MOTOR,
            motor_name or "Node Motor / Pump",
            location,
            "",
            TYPE_DISABLED,
            0,
            motor_name or "Node Motor / Pump",
            node_parent_key=node_parent_key,
            gpio_pin=gpio_pin,
        )
        flash("Node motor / pump added.", "success")
        return redirect(url_for("garden_detail", garden_id=garden_id))

    flash("Choose whether you are adding a sensor or motor / pump.", "danger")
    return redirect(url_for("garden_detail", garden_id=garden_id))


@app.route("/garden/<int:garden_id>/device/<int:device_id>/delete", methods=["POST"])
def delete_garden_device(garden_id, device_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        flash("Garden not found.", "danger")
        return redirect(url_for("garden"))

    try:
        with get_db_connection() as conn:
            conn.execute(
                "DELETE FROM devices WHERE id = ? AND garden_id = ?",
                (device_id, garden_id),
            )
        sync_garden_interlock_persistence(garden_id, garden, get_devices_for_garden(garden_id))
        flash("Device deleted.", "success")
    except Exception as e:
        flash(f"Failed to delete device: {e}", "danger")

    return redirect(url_for("garden_detail", garden_id=garden_id))


# ---------------- Settings actions ----------------
@app.route("/settings/save", methods=["POST"])
def settings_save():
    current = get_current_config()

    new_channels = {}
    for ch in CHANNELS:
        enabled = request.form.get(f"ch{ch}_enabled") == "on"
        typ = normalize_sensor_type(request.form.get(f"ch{ch}_type", TYPE_DISABLED))
        if typ not in VALID_TYPES:
            typ = TYPE_DISABLED
        new_channels[ch] = {"enabled": enabled, "type": typ}

    current["channels"] = new_channels
    save_current_config(current)

    flash("Settings saved.", "success")
    return redirect(url_for("settings_page"))


@app.route("/schedule/save", methods=["POST"])
def save_schedule():
    next_url = (request.form.get("next_url") or "").strip()
    garden_id_raw = (request.form.get("garden_id") or "").strip()
    try:
        garden_id = int(garden_id_raw)
    except Exception:
        garden_id = None

    if garden_id is None:
        flash("Schedules are now stored per garden. Choose a garden and use its schedule form.", "warning")
        if next_url and next_url.startswith("/"):
            return redirect(next_url)
        return redirect(url_for("schedule_page"))

    return save_garden_schedule(garden_id)


@app.route("/schedule/toggle", methods=["POST"])
def schedule_toggle():
    data = request.get_json(silent=True) or request.form
    garden_id_raw = data.get("garden_id")
    try:
        garden_id = int(garden_id_raw)
    except Exception:
        garden_id = None

    if garden_id is None:
        return jsonify(
            {
                "success": False,
                "error": "Schedules are now per-garden. Call /garden/<garden_id>/schedule/toggle or include garden_id.",
            }
        ), 400

    return garden_schedule_toggle(garden_id)


@app.route("/garden/<int:garden_id>/schedule/save", methods=["POST"])
def save_garden_schedule(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        flash("Garden not found.", "danger")
        return redirect(url_for("garden"))

    next_url = (request.form.get("next_url") or "").strip()

    pump_mode = str(request.form.get("schedule_pump_mode", "on")).lower()
    if pump_mode not in VALID_SCHEDULE_PUMP_MODES:
        pump_mode = "on"

    pump_cfg = garden.get("pump_config") or {}
    current_schedule = pump_cfg.get("schedule", {})
    sched = {
        "enabled": current_schedule.get("enabled", False),
        "start": request.form.get("schedule_start", "06:00:00"),
        "stop": request.form.get("schedule_stop", "06:15:00"),
        "pump_mode": pump_mode,
        "autowater_enabled": request.form.get("schedule_autowater_enabled") == "on",
        "days": {d: (request.form.get(f"day_{d}") == "on") for d in DAY_KEYS},
    }

    try:
        update_garden_pump_control(
            garden_id,
            schedule=sched,
            auto_water_enabled=request.form.get("auto_water_enabled") == "on",
        )
        flash("Garden schedule saved.", "success")
    except Exception as e:
        flash(f"Failed to save garden schedule: {e}", "danger")

    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for("garden_detail", garden_id=garden_id))


# ---------------- API routes ----------------
@app.route("/status")
def status():
    user_id = session.get("user_id")
    garden_id_raw = (request.args.get("garden_id") or "").strip()
    gardens = get_all_gardens(user_id)
    garden_statuses = get_dashboard_garden_statuses(user_id)
    selected_payload = None

    if garden_id_raw:
        try:
            garden_id = int(garden_id_raw)
        except Exception:
            return jsonify({"success": False, "error": "Invalid garden_id"}), 400
        garden = get_garden_by_id(garden_id, user_id)
        if not garden:
            return jsonify({"success": False, "error": "Garden not found"}), 404
        selected_payload = build_garden_status_payload(garden, get_devices_for_garden(garden_id))
    elif len(garden_statuses) == 1:
        selected_payload = garden_statuses[0]

    summary = build_dashboard_summary(garden_statuses)
    wifi_devices = []
    for d in get_all_wifi_devices(user_id):
        item = dict(d)
        item["wifi_status"] = pico_status_for_device(item)
        wifi_devices.append(item)

    return jsonify(
        {
            "success": True,
            "result": (selected_payload or {}).get("result"),
            "garden": (selected_payload or {}).get("garden"),
            "selected_garden_id": ((selected_payload or {}).get("garden") or {}).get("id"),
            "garden_statuses": garden_statuses,
            "summary": summary,
            "gardens": gardens,
            "wifi_devices": wifi_devices,
            "soil_range": {
                "wet": round(top_hat_soil_scale(TYPE_MOISTURE_RESISTIVE)["wet_v"], 3),
                "dry": round(top_hat_soil_scale(TYPE_MOISTURE_RESISTIVE)["dry_v"], 3),
            },
            "pump_on_below_v": round(top_hat_soil_scale(TYPE_MOISTURE_RESISTIVE)["pump_on_v"], 3),
            "ir_thresholds": {
                "present_max_v": round(top_hat_output_voltage(IR_WATER_PRESENT_MAX_V), 3),
                "refill_min_v": round(top_hat_output_voltage(IR_NEEDS_REFILL_MIN_V), 3),
            },
            "logged_in": bool(user_id),
            "username": session.get("username"),
            "mock": MOCK,
            "legacy_route": True,
            "message": "Status is now garden-scoped. Use /garden/<garden_id>/status for a specific garden or /dashboard/status for aggregate status.",
        }
    )


@app.route("/dashboard/status")
def dashboard_status():
    user_id = session.get("user_id")
    garden_statuses = get_dashboard_garden_statuses(user_id)
    summary = build_dashboard_summary(garden_statuses)
    return jsonify(
        {
            "garden_statuses": garden_statuses,
            "summary": summary,
            "logged_in": bool(user_id),
            "username": session.get("username"),
            "mock": MOCK,
        }
    )


@app.route("/garden/<int:garden_id>/status")
def garden_status(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        return jsonify({"success": False, "error": "Garden not found"}), 404

    devices = get_devices_for_garden(garden_id)
    payload = build_garden_status_payload(garden, devices)
    return jsonify({"success": True, **payload})


@app.route("/garden/<int:garden_id>/history")
def garden_history(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        return jsonify({"success": False, "error": "Garden not found"}), 404

    history_range = _normalize_history_range(request.args.get("range"))
    point_limit_raw = request.args.get("limit")
    try:
        point_limit = int(point_limit_raw) if point_limit_raw is not None else MAX_HISTORY_POINTS
    except Exception:
        point_limit = MAX_HISTORY_POINTS

    payload = build_garden_history_payload(garden, history_range=history_range, point_limit=point_limit)
    return jsonify(
        {
            "success": True,
            "garden": {"id": garden.get("id"), "name": garden.get("name")},
            **payload,
        }
    )


@app.route("/garden/<int:garden_id>/schedule/toggle", methods=["POST"])
def garden_schedule_toggle(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        return jsonify({"success": False, "error": "Garden not found"}), 404

    data = request.get_json(silent=True) or request.form
    enabled = str(data.get("enabled", "false")).lower() in ("1", "true", "yes", "on")
    pump_cfg = garden.get("pump_config") or {}
    current_schedule = dict(pump_cfg.get("schedule", {}))
    current_schedule["enabled"] = enabled

    update_garden_pump_control(garden_id, schedule=current_schedule)
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        return jsonify({"success": False, "error": "Garden not found after update"}), 404
    stat = compute_cycle_status(build_garden_cycle_config(garden, get_devices_for_garden(garden_id)))
    schedule_enabled = (garden.get("pump_config") or {}).get("schedule", {}).get("enabled", False)
    return jsonify(
        {
            "success": True,
            "schedule_enabled": schedule_enabled,
            "result": stat,
        }
    )


@app.route("/auto_water", methods=["POST"])
def auto_water_toggle():
    data = request.get_json(silent=True) or request.form
    garden_id_raw = data.get("garden_id")
    try:
        garden_id = int(garden_id_raw)
    except Exception:
        garden_id = None

    if garden_id is None:
        return jsonify(
            {
                "success": False,
                "error": "Auto-water is now per-garden. Call /garden/<garden_id>/auto_water or include garden_id.",
            }
        ), 400

    return garden_auto_water_toggle(garden_id)


@app.route("/garden/<int:garden_id>/auto_water", methods=["POST"])
def garden_auto_water_toggle(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        return jsonify({"success": False, "error": "Garden not found"}), 404

    data = request.get_json(silent=True) or request.form
    enabled = str(data.get("enabled", "false")).lower() in ("1", "true", "yes", "on")

    update_garden_pump_control(garden_id, auto_water_enabled=enabled)
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        return jsonify({"success": False, "error": "Garden not found after update"}), 404
    stat = compute_cycle_status(build_garden_cycle_config(garden, get_devices_for_garden(garden_id)))
    auto_water_enabled = (garden.get("pump_config") or {}).get("auto_water_enabled", False)
    return jsonify(
        {
            "success": True,
            "auto_water_enabled": auto_water_enabled,
            "result": stat,
        }
    )


@app.route("/pump/manual", methods=["POST"])
def pump_manual():
    data = request.get_json(silent=True) or request.form
    garden_id_raw = data.get("garden_id")
    try:
        garden_id = int(garden_id_raw)
    except Exception:
        garden_id = None

    if garden_id is None:
        return jsonify(
            {
                "success": False,
                "error": "Manual pump override is now per-garden. Call /garden/<garden_id>/pump/manual or include garden_id.",
            }
        ), 400

    return garden_pump_manual(garden_id)


@app.route("/garden/<int:garden_id>/pump/manual", methods=["POST"])
def garden_pump_manual(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        return jsonify({"success": False, "error": "Garden not found"}), 404

    data = request.get_json(silent=True) or request.form
    enabled = str(data.get("enabled", "false")).lower() in ("1", "true", "yes", "on")
    requested_on = str(data.get("requested_on", "false")).lower() in ("1", "true", "yes", "on")

    cfg_snapshot = build_garden_cycle_config(garden, get_devices_for_garden(garden_id))
    stat = compute_cycle_status(cfg_snapshot)

    if stat["interlock"]["enabled"] and enabled and requested_on and stat["interlock"]["active"]:
        return jsonify(
            {
                "success": False,
                "error": "Pump ON blocked by interlock",
                "interlock": stat["interlock"],
            }
        ), 400

    update_garden_pump_control(
        garden_id,
        manual={"enabled": enabled, "requested_on": requested_on},
    )
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        return jsonify({"success": False, "error": "Garden not found after update"}), 404
    stat = compute_cycle_status(build_garden_cycle_config(garden, get_devices_for_garden(garden_id)))
    manual_override = (garden.get("pump_config") or {}).get("manual", {})

    return jsonify(
        {
            "success": True,
            "manual_override": manual_override,
            "result": stat,
        }
    )


@app.route("/interlock", methods=["POST"])
def toggle_interlock():
    data = request.get_json(silent=True) or request.form
    user_id = session.get("user_id")
    garden_id_raw = str(data.get("garden_id", "") or "").strip()
    enabled = str(data.get("enabled", "true")).lower() in ("1", "true", "yes", "on")

    if not garden_id_raw:
        return jsonify(
            {
                "success": False,
                "error": "garden_id is required",
                "message": "Interlock is now stored per garden. Call /garden/<garden_id>/interlock or include garden_id.",
            }
        ), 400

    garden_statuses = get_dashboard_garden_statuses(user_id)
    selected_payload = None

    try:
        garden_id = int(garden_id_raw)
    except Exception:
        return jsonify({"success": False, "error": "Invalid garden_id"}), 400
    garden = get_garden_by_id(garden_id, user_id)
    if not garden:
        return jsonify({"success": False, "error": "Garden not found"}), 404

    cfg_snapshot = build_garden_cycle_config(garden, get_devices_for_garden(garden_id))
    if enabled and not any(
        info.get("enabled") and is_interlock_sensor_type(info.get("type"))
        for info in cfg_snapshot.get("channels", {}).values()
    ):
        return jsonify(
            {
                "success": False,
                "error": "Interlock requires a water sensor",
                "message": "Interlock can only be enabled when this garden has a water level sensor.",
            }
        ), 400

    update_garden_pump_control(garden_id, interlock_enabled=enabled)
    garden = get_garden_by_id(garden_id, user_id)
    selected_payload = build_garden_status_payload(garden, get_devices_for_garden(garden_id))
    garden_statuses = get_dashboard_garden_statuses(user_id)

    return jsonify(
        {
            "success": True,
            "interlock_enabled": bool((selected_payload or {}).get("result", {}).get("interlock", {}).get("enabled")),
            "result": (selected_payload or {}).get("result"),
            "garden": (selected_payload or {}).get("garden"),
            "selected_garden_id": ((selected_payload or {}).get("garden") or {}).get("id"),
            "garden_statuses": garden_statuses,
            "summary": build_dashboard_summary(garden_statuses),
            "mock": MOCK,
            "legacy_route": True,
            "message": "Interlock is stored per garden. Aggregate garden status is included for dashboard refresh.",
        }
    )


@app.route("/garden/<int:garden_id>/interlock", methods=["POST"])
def garden_interlock_toggle(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        return jsonify({"success": False, "error": "Garden not found"}), 404

    data = request.get_json(silent=True) or request.form
    enabled = str(data.get("enabled", "true")).lower() in ("1", "true", "yes", "on")
    cfg_snapshot = build_garden_cycle_config(garden, get_devices_for_garden(garden_id))

    if enabled and not any(
        info.get("enabled") and is_interlock_sensor_type(info.get("type"))
        for info in cfg_snapshot.get("channels", {}).values()
    ):
        return jsonify(
            {
                "success": False,
                "error": "Interlock requires a water sensor",
                "message": "Interlock can only be enabled when this garden has a water level sensor.",
            }
        ), 400

    update_garden_pump_control(garden_id, interlock_enabled=enabled)
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    stat = compute_cycle_status(build_garden_cycle_config(garden, get_devices_for_garden(garden_id)))

    return jsonify(
        {
            "success": True,
            "interlock_enabled": stat["interlock"]["enabled"],
            "result": stat,
        }
    )


@app.route("/garden/<int:garden_id>/pico/status")
def garden_pico_status(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        return jsonify({"success": False, "error": "Garden not found"}), 404

    devices = get_devices_for_garden(garden_id)
    nodes = get_garden_wifi_nodes(garden_id, garden=garden, devices=devices)
    results = []
    for node in nodes:
        node_parent_key = build_node_parent_key(node, garden_id)
        node_status = pico_status_for_device(node)
        node_payload = node_status.get("data") if isinstance(node_status, dict) else {}
        configured_devices = []
        for child in get_configured_node_devices(garden_id, node_parent_key):
            child_sensor_type = child.get("sensor_type") or TYPE_DISABLED
            child_device_type = child.get("device_type") or ""
            child_gpio_pin = child.get("gpio_pin")
            child_reading = None
            child_status_text = "No live data"
            child_voltage_text = "--"
            if child_device_type == DEVICE_SENSOR:
                child_reading = normalize_node_sensor_reading(
                    node_payload,
                    child_sensor_type,
                    child_gpio_pin,
                    node.get("device_type"),
                )
                child_status_text = _sensor_status_text(child_sensor_type, child_reading)
                child_voltage_text = _sensor_voltage_text(child_reading)
            configured_devices.append({
                "id": child.get("id"),
                "device_type": child_device_type,
                "sensor_type": child_sensor_type,
                "label": child.get("sensor_name") or child.get("device_label") or "Configured Device",
                "gpio_pin": child_gpio_pin,
                "binding_label": _node_sensor_binding_label(
                    node.get("device_type"),
                    child_gpio_pin,
                    sensor_type=child_sensor_type,
                    device_type=child_device_type,
                ),
                "reading": child_reading,
                "status_text": child_status_text,
                "voltage_text": child_voltage_text,
            })
        results.append({
            "id": node.get("id"),
            "source": node.get("source"),
            "node_parent_key": node_parent_key,
            "label": node.get("device_label") or node.get("sensor_name") or "WiFi Node",
            "device_type": node.get("device_type") or "",
            "ip_address": node.get("ip_address") or "",
            "location": node.get("location") or "",
            "sensor_type": node.get("sensor_type") or "",
            "channel": node.get("channel"),
            "configured_devices": configured_devices,
            "status": node_status,
        })
    return jsonify({"success": True, "nodes": results})

@app.route("/garden/<int:garden_id>/pico/gpio", methods=["POST"])
def garden_pico_gpio_write(garden_id):
    garden = get_garden_by_id(garden_id, session.get("user_id"))
    if not garden:
        return jsonify({"success": False, "error": "Garden not found"}), 404
    data = request.get_json(silent=True) or request.form
    ip_address = normalize_ip_address(data.get("ip_address", ""))
    try:
        pin = int(data.get("pin"))
        state = 1 if str(data.get("state", "0")).lower() in ("1", "true", "on", "yes", "high") else 0
    except Exception:
        return jsonify({"success": False, "error": "Invalid pin/state"}), 400
    allowed_ips = set()
    if garden.get("device_type") == DEVICE_PICO and garden.get("ip_address"):
        allowed_ips.add(normalize_ip_address(garden.get("ip_address")))
    for d in get_wifi_devices_for_garden(garden_id):
        if d.get("ip_address"):
            allowed_ips.add(normalize_ip_address(d.get("ip_address")))
    if not ip_address or ip_address not in allowed_ips:
        return jsonify({"success": False, "error": "Pico W IP address is not registered to this garden"}), 400
    result = pico_write_gpio(ip_address, pin, state)
    http_code = 200 if result.get("online") else 502
    return jsonify({"success": bool(result.get("online")), "result": result}), http_code

# ---------------- Diagnostics ----------------
@app.route("/health")
def health():
    ok = True
    issues = []

    try:
        with get_db_connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _health (x INTEGER)")
    except Exception as e:
        ok = False
        issues.append(f"DB problem: {e}")

    required_templates = [
        "base.html",
        "login.html",
        "register.html",
        "dashboard.html",
        "garden.html",
        "garden_detail.html",
        "schedule.html",
        "settings.html",
        "help.html",
    ]

    for t in required_templates:
        if not os.path.exists(os.path.join(BASE_DIR, "templates", t)):
            ok = False
            issues.append(f"Missing template: {t}")

    if MOCK:
        issues.append("MOCK hardware enabled")
    if hw_errors:
        issues.extend(hw_errors[-5:])

    return jsonify({"ok": ok, "issues": issues, "db_path": DB_PATH})


@app.route("/diag")
def diag():
    info = {
        "base_dir": BASE_DIR,
        "db_path": DB_PATH,
        "db_exists": os.path.exists(DB_PATH),
        "mock": MOCK,
        "hw_errors": hw_errors[-10:],
        "env": {
            "FLASK_SECRET_KEY_set": bool(os.environ.get("FLASK_SECRET_KEY")),
            "GARDENPI_MOCK": os.environ.get("GARDENPI_MOCK"),
        },
    }

    try:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            info["db_tables"] = [r[0] for r in cur.fetchall()]
    except Exception as e:
        info["db_error"] = str(e)

    return jsonify(info)


# ---------------- Main ----------------
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=bool(os.environ.get("FLASK_DEBUG")),
        threaded=True,
    )