# Sensor library

One folder per sensor. Every folder not starting with `_` is loaded at server start and checked by `sensors.py`; a bad file stops the server with an error.

```
sensors/
  schema.json          shape of sensor.json (also usable by editors / jsonschema)
  _template/           copy this to start a new sensor
  <id>/sensor.json     structured facts: identity, pins, link, framing, board APIs, fields, views, test plan
  <id>/knowledge.md    datasheet notes for the LLM (registers, procedures, failure signatures)
```

## What each part is used for
| Part | Used by |
|---|---|
| `model`, `aliases` | `lookup_sensor` matching what the user types |
| `interface` (`ttl` / `rs485`) | which free probe cube port the sensor is given (uart4 → usart6 for TTL, usart2 for RS485) |
| `pins[].signal` | the wiring plan: `UART_TX`→port RX pin, `UART_RX`→port TX pin, `VCC_5V`/`VCC_3V3`→power pin, `GND`→GND, `RS485_A/B`→transceiver A/B |
| `framing` | how received bytes become readings: `line` (CRLF text, `pattern` marks data lines, `KEY=value` pairs become fields) or `witmotion` (binary 0x55 frames) |
| `stream` | how sampling starts and stops (`start_api` / `stop_api`), or `continuous: true` |
| `apis` | sensor commands added to `call_board_api` for this component (same format as `board_config.json`; `{port}` is the assigned port). `pre`/`post` run other APIs around it, `verify_stream` listens for valid readings afterwards |
| `verify_apis` | what configuration runs to prove the link (`ports`, `sensor_ping`, `stream_probe`, ...) |
| `recommended_settings` | what configuration applies first (keys match API params) |
| `fields` | units and ranges for charts and the LLM |
| `views` | what the UI draws, in order: `attitude` (live 3D plane: `roll`/`pitch`/`yaw` fields), `timeseries` (`fields` in one plot), `motion` (`level`/`flag`), `stat` (latest value). Without `views`, each field gets its own small chart |
| `ui_actions`, `sim_action` | buttons on the live cards (e.g. Zero yaw) and the sim bench button (e.g. Simulate tilt) |
| `test_plan` | the Testing phase; `offline_check` is what offline mode uses to pass/fail |
| `knowledge.md` | added to the prompt once this sensor is identified |

## Adding a sensor
1. `cp -r _template <id>` and fill in `sensor.json` and `knowledge.md`.
2. If it speaks a new binary protocol, add a decoder to `../decoders.py` and a `framing.type` for it.
3. To try it on the simulated bench, add a device to `../sim.py` and set `sim_device`.
4. Restart the server and type the model name in the Connectivity phase.
