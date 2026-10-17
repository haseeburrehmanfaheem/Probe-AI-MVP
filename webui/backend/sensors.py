"""Sensor library: sensors/<id>/sensor.json + knowledge.md, loaded and checked at start-up.

A bad definition raises at import, so a typo shows up when the server starts rather than
halfway through a bring-up. jsonschema (optional) also checks sensors/schema.json.
"""
import json, re
from pathlib import Path

DIR = Path(__file__).parent / "sensors"
SIGNALS = {"UART_TX", "UART_RX", "GND", "VCC_5V", "VCC_3V3", "RS485_A", "RS485_B"}
FRAMINGS = {"line", "witmotion"}
VIEW_KINDS = {"timeseries", "motion", "attitude", "stat"}
BOARD_HELPERS = {"stream_probe"}  # board-side APIs every sensor can use (see board.py)
PROMPT_HIDDEN = {"test_plan", "views", "sim_action", "ui_actions", "ui_rate_hz", "sim_device", "_id", "_knowledge"}


class SensorError(ValueError): pass


def _check(s, sid, board_apis):
    def need(cond, msg):
        if not cond: raise SensorError(f"sensors/{sid}/sensor.json: {msg}")
    for k in ("model", "type", "protocol", "interface", "pins", "framing", "fields", "test_plan"):
        need(k in s, f"missing '{k}'")
    need(s["interface"] in ("ttl", "rs485"), "interface must be ttl or rs485")
    for p in s["pins"]: need(p.get("signal") in SIGNALS, f"pin {p.get('name')}: signal must be one of {sorted(SIGNALS)}")
    need(s["framing"].get("type") in FRAMINGS, f"framing.type must be one of {sorted(FRAMINGS)}")
    if s["framing"]["type"] == "line":
        need("pattern" in s["framing"], "line framing needs a pattern"); re.compile(s["framing"]["pattern"])
    apis = {**board_apis, **s.get("apis", {}), **{h: {} for h in BOARD_HELPERS}}
    for name, a in s.get("apis", {}).items():
        need(name not in board_apis, f"api {name} clashes with a board API")
        for k in ("description", "command", "expect"): need(k in a, f"api {name}: missing '{k}'")
        for step in a.get("pre", []) + a.get("post", []):
            need((isinstance(step, dict) and "sleep" in step) or (isinstance(step, str) and step in apis),
                 f"api {name}: unknown pre/post step {step!r}")
    stream = s.get("stream", {})
    for k in ("start_api", "stop_api"):
        if k in stream: need(stream[k] in apis, f"stream.{k} {stream[k]!r} is not an API")
    for n in s.get("verify_apis", []): need(n in apis, f"verify_apis: unknown API {n!r}")
    for a in s.get("ui_actions", {}).values(): need(a.get("api") in apis, f"ui_actions: unknown API {a.get('api')!r}")
    fields = set(s["fields"])
    for v in s.get("views", []):
        need(v.get("kind") in VIEW_KINDS, f"view kind must be one of {sorted(VIEW_KINDS)}")
        used = v.get("fields", []) + [v[k] for k in ("roll", "pitch", "yaw", "level", "flag") if k in v]
        for f in used: need(f in fields, f"view {v.get('title', v['kind'])}: unknown field {f!r}")
    for t in s["test_plan"]:
        for k in ("name", "instruction", "seconds", "checks"): need(k in t, f"test {t.get('name')}: missing '{k}'")


def load(board_apis=None):
    schema = json.loads((DIR / "schema.json").read_text())
    try: import jsonschema
    except ImportError: jsonschema = None
    out = []
    for d in sorted(p for p in DIR.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        f = d / "sensor.json"
        if not f.exists(): continue
        try: s = json.loads(f.read_text())
        except ValueError as e: raise SensorError(f"sensors/{d.name}/sensor.json: {e}") from None
        if jsonschema: jsonschema.validate(s, schema)
        _check(s, d.name, board_apis or {})
        k = d / "knowledge.md"
        s["_id"], s["_knowledge"] = d.name, k.read_text() if k.exists() else ""
        out.append(s)
    return out


def public(s):
    """What the LLM sees about a sensor: everything except UI/test/sim plumbing."""
    return {k: v for k, v in s.items() if k not in PROMPT_HIDDEN}


def units(s): return {k: f.get("unit", "") for k, f in s.get("fields", {}).items()}
