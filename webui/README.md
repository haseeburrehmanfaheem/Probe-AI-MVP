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
    board.py               board APIs from board_config.json, sampling, cfg restore
    console.py             Nautilus console protocol: echo/prompt framing, RX burst decoding, 'ports' parsing
    sim.py                 simulated bench: the console firmware + MEGA dummy sensor, byte for byte
    board_config.json      transport, board APIs, sampling settings, probe cube ports
    sensors/*.json         datasheet summaries; sensors/unsupported/ is not loaded
    prompts/*.txt          shared persona prompt plus one prompt per phase
  frontend/                index.html, app.js, style.css (served at /static)
```

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

## How the backend talks to the board
- Every command is sent as one line ending in `\r`. The reply is everything between the echo and the next `> `. If the echo doesn't match what was sent, the command is retried.
- An API succeeds only if a reply line matches its `expect` regex. Every other reply counts as a failure.
- `send` APIs (`sensor_expect`) then wait for the sensor's answer. The answer arrives in hex RX bursts, which are decoded per port and split on CRLF.
- If a `cfg` fails at port level (`ERR uart4: ...`), the firmware has still stored the bad value. The backend re-applies the last good config so the next `cfg` doesn't silently inherit it.
- Sampling reads `ports` before START and after STOP. That gives link counter deltas without printing a long table while frames stream in.

## Using the real board
1. In `backend/board_config.json`, set `transport.type` to `"serial"`.
2. Set `transport.port` to the ST-LINK VCP (`ls /dev/cu.usbmodem*`) or a forwarded port such as `socket://<ip>:7000`.
3. Flash the STM32 and the MEGA as described in `UART_LAB_GUIDE.md`. Wire MEGA TX1 (pin 18) → PD0, RX1 (pin 19) → PD1, plus GND.
4. Close any other serial terminal on the VCP. Only one program can own it.

To support another device, add `sensors/<model>.json` and the APIs it needs in `board_config.json`. Each API is a console command template whose `{port}` comes from `vars`, plus an `expect` regex and, for sensor commands, a `sensor_expect` regex.
