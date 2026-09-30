# Probe web UI

A guided sensor bring-up UI with three LLM-driven phases per component: Connectivity, Configuration and Testing.

```bash
pip install -r ../requirements.txt
export OPENAI_API_KEY=...            # optional: OPENAI_MODEL=<model id>
python server.py                     # http://127.0.0.1:8000
```

## Testing without OpenAI
Use the **Offline / Online** switch at the top right. A badge shows the current mode.
- **Offline:** `offline_llm.py` stands in for the LLM. It is a rule-based script per phase that calls the same tools and passes the same gates.
- **Online:** uses OpenAI (`OPENAI_MODEL`). If the server has no `OPENAI_API_KEY`, the UI asks for a key. The key is checked, then kept in server memory only and never written to disk.
- The server starts in online mode when a key is set, otherwise offline. `PROBE_LLM=offline` forces offline.
- You can switch between components, but not while one is in progress.

Things to try in offline mode:
- a wrong model number, then `wt61pc`
- `set baud to 57600` during configuration: the sim sensor goes silent and the still test fails
- `set baud to 9600` after that failure, to recover
- running the motion test without ticking "Simulate motion": it fails; tick the box and say `retry`

## Files
- `board_config.json`: the board, its transport, the **board APIs**, the sampling settings and the probe cube port map.
- `sensors/*.json`: hardcoded datasheet summaries (pins, UART defaults, test plan). `wt61pc.json` is the demo sensor.
- `prompts/*.txt`: the shared persona prompt plus one prompt per phase. `{fields}` are filled from the configs and the session state.
- `agent.py`: the phase loop and tools. The LLM leaves a phase only through `complete_phase`, whose gate is checked in code (all pins confirmed, config applied and recorded, all tests passed).
- `board.py`: the serial transport, the simulated board, and the command/sample client.

## Switching to the real Nucleo
1. In `board_config.json`, set `transport.type` to `"serial"` and `transport.port` to the ST-LINK VCP (`ls /dev/cu.usbmodem*`).
2. Replace the entries under `apis` with your firmware's commands:
   - `command`: a template, where `{param}` is filled from `params`.
   - `expect`: a regex for the success reply. `error_pattern` catches failures.
   - `description` and `params`: these are what the LLM sees, so describe each API the way you would explain it to a person.
3. Set `sampling.start_api` and `stop_api` (either can be `null` if data streams continuously) and `data_line_pattern`. Data lines are parsed as `KEY=value` pairs.
4. Set `probe_cube_ports`. `accepts` must match a sensor pin's `signal` (for example, the port that is the MCU RX accepts `UART_TX`).

The sim board only produces data at 9600 8N1. If configuration picks other settings, the test gets no samples and the LLM has to diagnose it. The sidebar checkbox "Simulate motion" stands in for moving the sensor.
