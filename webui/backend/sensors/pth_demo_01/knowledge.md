# PTH-DEMO-01: notes for the assistant

A placeholder name for a pressure / temperature / humidity sensor on the bench. It was characterised by listening to it on the
probe cube (uart4, 2026-10-17). The sensor chip and any command set are not identified yet; treat anything beyond the observations below as unknown.

## Link (measured)
- 9600 8N1. 40 s of listening gave 540 bytes with ORE, FE, NE and PE all 0.
- It streams continuously from power-up at **0.5 Hz (one line every 2 s)**. There is no start/stop, ping or ID command that we know of.
  **Do not send it commands.** The only way to check it works is to listen: use `stream_probe` (it listens 6 s by default for this sensor).
- Line format: `T:<°C>,H:<%RH>,P:<hPa>` + CRLF, 2 decimals, 27 bytes, e.g. `T:16.50,H:24.80,P:1019.55`.
  The backend parses it into fields T (°C), H (%), P (hPa).
- On the bench it is driven by an Arduino Uno R3 (its USB also appears on the host as a second serial port). That port is the Arduino's, not the probe cube's.

## Values seen (indoor bench, 20 readings)
- T 15.0–16.3 °C, H 24.9–27.3 %, P 1012.4–1020.0 hPa.
- Pressure moved up to ~1.9 hPa between consecutive readings, and humidity up to ~1 %. A real barometer is steady to about 0.1 hPa over seconds,
  so these readings are either simulated or come from a noisy source. Mention it if the user asks about accuracy. It is not a link fault.

## Failure signatures
- No lines at all: TX wire (sensor TX → PD0), GND, or the sensor/Arduino isn't powered.
- Garbled lines or FE climbing: the port baud isn't 9600.
- Lines that don't match the format: a different sketch/firmware is running on the sensor side.
- Fewer readings than expected: remember it only sends every 2 s; listen at least 6 s.
