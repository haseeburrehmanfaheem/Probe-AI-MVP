# Probe web UI

A guided sensor bring-up UI with three LLM-driven phases per component: Connectivity, Configuration and Testing.
It drives the **Nautilus UART LAB** firmware (`nautilus_PTH_test`, STM32 Nucleo-H723ZG) through its USART3 console,
with the Arduino MEGA dummy sensor (`tools/dummy_sensor/dummy_sensor.ino`) on uart4.

```bash
pip install -r ../requirements.txt
export OPENAI_API_KEY=...            # optional: OPENAI_MODEL=<model id>
python backend/server.py             # http://127.0.0.1:8000
```

## Layout
```
webui/
  backend/                 Python server (FastAPI + WebSocket)
    server.py              serves the frontend, one agent Session per tab
    agent.py               phase loop, tools and the gates for leaving a phase
    offline_llm.py         rule-based stand-in for the LLM (offline mode)
    board.py               board + sensor APIs, stream_probe, sampling and the live stream, cfg restore
    console.py             Nautilus console protocol: echo/prompt framing, RX burst decoding, 'ports' parsing
    decoders.py            per-port byte decoders: CRLF text lines, WitMotion binary frames
    sim.py                 simulated bench: the console firmware + sensors on any lab port, byte for byte
    board_config.json      transport and the generic port APIs (cfg, ports, stream_probe, ...)
    boards/*.json          probe cube hardware: lab ports, pins, power, console commands (from nautilus_PTH_test)
    sensors/<id>/          sensor library: sensor.json + knowledge.md per sensor (see sensors/README.md)
    sensors.py             loads and checks the sensor library
    prompts/*.txt          shared persona prompt plus one prompt per phase
  frontend/                index.html, app.js, style.css (served at /static)
```

## Projects
The sidebar has **New project** at the top, then **Pinned** and **History**, then the open project's System tree.
- Each project's UI events (messages, schematics, board console, results) are saved to `backend/data/projects/<id>.json`. Set `PROBE_DATA` to store them somewhere else.
- Opening a project from the list replays it into the UI. **Add a component** continues it, with numbering carrying on.
- A component left unfinished (page reload, server restart) is shown as **Interrupted**. Add a new component to redo it.
- Hover a project to pin, unpin or delete it.

## Testing without OpenAI
Use the **Offline / Online** switch at the top right. A badge shows the current mode.
- **Offline:** `offline_llm.py` stands in for the LLM. It is a rule-based script per phase that calls the same tools and passes the same gates.
- **Online:** uses OpenAI (`OPENAI_MODEL`). If the server has no `OPENAI_API_KEY`, the UI asks for a key. The key is checked, then kept in server memory only and never written to disk.
- The server starts in online mode when a key is set, otherwise offline. `PROBE_LLM=offline` forces offline.

Things to try with the sim board (model: `nautilus dummy sensor`):
- During configuration, `set baud to 19200`. The console accepts it, but the sensor stops answering and FE climbs. `set baud to 9600` recovers.
- `set parity to even`. The sensor can't parse 8E1 commands, so the ping fails.
- Tick "Simulate a loose sensor TX wire" before a test. The stream never starts and the test fails. Untick it and say `retry`.
- Watch the board log after any reconfiguration. The first sensor command gets `ERR unknown cmd` (stale bytes in the MEGA's line buffer), and the backend retries it once.

## Probe cube ports
The hardware is described in `backend/boards/probe_cube_h723.json`, taken from the `nautilus_PTH_test` firmware.
It has three lab ports: `uart4` (TTL, TX PD1 / RX PD0), `usart6` (TTL, TX PC6 / RX PC7) and `usart2` (RS485 transceiver, DE PE4), plus 5V, 3V3 and GND.
- When a sensor is identified, the component gets the first free port matching the sensor's `interface`: `uart4`, then `usart6` for TTL, and `usart2` for RS485.
  The wiring plan, every board API and the sim bench use that port.
- The prompt carries a "Probe cube status" with each port's pins and which component uses it. The LLM uses it to tell the user where the sensor goes and which port is still free for the next one.
  The "done" card shows the same hint.
- Ports used by finished components of a project stay taken when the project is reopened.

## Live views
Each sensor declares its `views` in `sensor.json`: `attitude` (a live 3D plane following roll/pitch/yaw), `timeseries`, `motion` or `stat`.
- After configuration, Probe opens a **Live monitor** card. It keeps streaming through later settings changes and tests:
  board commands and test measurements run inside the same stream. It stops when you press Stop or add the next sensor.
- When bring-up finishes, a fresh live card is shown at the end and keeps streaming (the earlier card says "Continued below").
- Every test card shows the same views while it samples. Chart/Table switch as before.
- Hovering a chart scrubs through its samples. While data is live, the plane always shows the latest pose; once the stream stops, hovering moves the plane to the hovered moment.

## Configuration and tests
- Probe sets the probe cube port itself. Settings that write the sensor (APIs marked `changes_sensor`, e.g. the WT61PC's rate) are proposed with the current value, the recommended value and the reason (`recommended_notes`). They are applied only if you agree.
- Each test's pass criteria are `checks` in `sensor.json`, evaluated in code (`backend/checks.py`). The LLM gets the pass/fail list and can't record a pass while a check failed.
- Buttons on the cards come from the sensor too: `ui_actions` (e.g. **Zero yaw**) and, on the sim bench, `sim_action` (e.g. **Simulate tilt**).

## WT61PC-TTL (accelerometer)
Enter the model `WT61PC` to bring up the WitMotion 6-axis IMU on `uart4`: VCC→5V, TX→PD0, RX→PD1, GND→GND.
- It streams binary 0x55 frames continuously and never acknowledges commands. Configuration sets the port to 9600 8N1, sets the output rate (unlock, write, save) and proves the link with `stream_probe`.
- Sim bench: **Simulate tilt** sweeps roll, pitch and yaw for 5 s. The tilt and rotate tests need it pressed during the measurement.
- Try `rate 20 hz` during configuration (the measured rate follows; the sim does not model it, but on the real probe cube 20 Hz at 9600 damages frames, so 10 Hz is the recommended rate). Try `set baud to 115200`: only the port changes, so frames stop. Then `set baud to 9600` recovers.
- Datasheet values that still need checking against the WitMotion manual are marked (verify) in `sensors/wt61pc_ttl/knowledge.md`.

## Motion sensor
Enter the model `motion sensor` (or `nautilus motion sensor` / `pir`) to bring up the motion sensor instead of the temperature/humidity one.
- **Sim board:** the simulated MEGA switches to the motion sketch. In the test card, **Simulate motion** makes the presence level rise for 2.5 s. The motion test needs it pressed during the measurement.
- **Chart:** presence level on a 0–1 axis, with motion periods shaded and a "Motion / Clear" label. Chart/Table switch.
- **Real hardware:** flash `tools/motion_sensor/motion_sensor.ino` to the MEGA with a PIR module (for example HC-SR501) on pin 2. It speaks the same commands as the dummy sensor and streams `$MOTN,L=<0.00-1.00>,M=<0|1>,SEQ=<n>*`.

## How the backend talks to the board
- Every command is sent as one line ending in `\r`. The reply is everything between the echo and the next `> `. If the echo doesn't match what was sent, the command is retried.
- An API succeeds only if a reply line matches its `expect` regex. Every other reply counts as a failure.
- `send` APIs (`sensor_expect`) then wait for the sensor's answer. The answer arrives in hex RX bursts, which are decoded per port and split on CRLF.
- If a `cfg` fails at port level (`ERR uart4: ...`), the firmware has still stored the bad value. The backend re-applies the last good config so the next `cfg` doesn't silently inherit it.
- Sampling reads `ports` before START and after STOP. That gives link counter deltas without printing a long table while frames stream in.

## Using the real board
```bash
ls /dev/cu.usbmodem*                                   # the ST-LINK VCP
PROBE_TRANSPORT=serial PROBE_PORT=/dev/cu.usbmodemXXXX python backend/server.py
```
- `PROBE_TRANSPORT` / `PROBE_PORT` override `transport` in `backend/board_config.json`, which stays on `sim`. A forwarded port such as `socket://<ip>:7000` also works.
- Flash the STM32 with `nautilus_PTH_test` (see `UART_LAB_GUIDE.md`). Use the version that clears console overruns: older builds can go deaf after a burst of input until reset.
- Close any other serial terminal (`screen`, Serial Monitor) on the VCP first. Only one program can own it.

## Debugging a session
Each project writes two files under `backend/data/` (or `$PROBE_DATA`):
- `projects/<id>.json`: what the UI showed (messages, cards, board console, readings). Opening the project replays it.
- `traces/<id>.jsonl`: what the LLM saw and did, one JSON line per event:
  - `prompt`: the full system prompt, written whenever it changes
  - `request`: input items, the tools offered, the previous response id
  - `response`: text, tool calls, token usage, latency
  - `tool`: arguments and the full result
  - plus `phase`, `ui` (buttons), `mode` and `error` events

  The API key is never written. `PROBE_TRACE=0` turns tracing off.

```bash
python backend/llm_trace.py backend/data/traces/<id>.jsonl          # readable transcript
python backend/llm_trace.py backend/data/traces/<id>.jsonl --full   # with full prompts and tool results
```
The project id is the file name. The newest one is `ls -t backend/data/traces | head -1`.

To support another device, copy `backend/sensors/_template/` to `backend/sensors/<id>/` and fill in `sensor.json` and `knowledge.md` (see `backend/sensors/README.md`).
Sensor commands live in the sensor's `apis`. Each is a console command template whose `{port}` is the component's assigned port, plus an `expect` regex and either a `sensor_expect` regex or `verify_stream` for sensors that never answer.
A new binary protocol also needs a decoder in `decoders.py`.
