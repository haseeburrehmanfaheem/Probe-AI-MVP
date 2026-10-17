# WT61PC-TTL (WitMotion): notes for the assistant

Values marked (verify) come from the WitMotion standard protocol shared by its 6/9-axis TTL modules. Confirm them
against the WT61PC-TTL manual from WitMotion before relying on them on real hardware.

## What it is
- 6-axis IMU: 3-axis accelerometer + 3-axis gyroscope with an onboard Kalman filter, which outputs attitude angles.
- Ranges: acceleration ±16 g, angular rate ±2000 °/s, Roll ±180°, Pitch ±90°, Yaw ±180°.
- Typical accuracy after calibration: Roll/Pitch about 0.05° static and 0.1° dynamic (verify).
- No magnetometer, so **yaw is relative and drifts slowly**. Zero it (wt_zero_yaw) before a yaw test. Never treat it as a compass heading.
- Supply 3.3-5 V. The UART is TTL, so it can connect straight to a probe cube TTL port (uart4 or usart6), with TX→RX and RX→TX crossed.

## Link
- Factory default 9600 8N1. The only other supported baud is 115200.
- It streams continuously from power-up. There is no start/stop, ping or ID command, and **it never acknowledges a command**.
  The only proof that it works is valid frames arriving: use `stream_probe`. It counts valid frames by type, checksum errors and the measured rate.
- Output rate: units can ship with different rates (one bench unit was found at 1 Hz), so read `rate_hz` from stream_probe before assuming anything.
- Bandwidth: each sample is 4 frames × 11 bytes = 44 bytes (51, 52, 53, plus an all-zero 54), about 440 bits. A 9600 link tops out near 21 samples/s.
- **Measured on the probe cube: 10 Hz at 9600 is clean** (0 checksum errors over 50+ samples). **20 Hz at 9600 is not**: about 10% of frames are damaged,
  because the probe cube's polled monitor overruns the lab port's FIFO while it prints. 115200 makes that worse, not better.
  Recommend 10 Hz or less. If the user asks for more, explain this limit. If they still want it, set it, then show them the checksum errors in stream_probe.

## Frames (sensor → cube)
Every frame is 11 bytes: `55 <type> d0 d1 d2 d3 d4 d5 d6 d7 <sum>`. Values are little-endian int16, and sum = low byte of the sum of the first 10 bytes.
| type | content | scaling |
|---|---|---|
| 0x51 | AX AY AZ, T | a = raw/32768 × 16 g; T = raw/100 °C (confirmed on hardware) |
| 0x52 | GX GY GZ | w = raw/32768 × 2000 °/s |
| 0x53 | Roll Pitch Yaw | angle = raw/32768 × 180° |
| 0x54 | magnetometer | always zero on the WT61PC (no magnetometer); ignored |
The backend decodes these and merges each 51/52/53 group into one sample with fields AX AY AZ T GX GY GZ Roll Pitch Yaw.

## Configuration (cube → sensor), sent with `sendhex <port> ...`
Each write is `FF AA <register> <low> <high>`. Writes only take effect after the unlock `FF AA 69 88 B5`, and they persist only after a save `FF AA 00 00 00`.
The wt_* board APIs send the unlock and the save for you.
| API | bytes | notes |
|---|---|---|
| wt_set_rate | FF AA 03 code 00 | 0.2→01, 0.5→02, 1→03, 2→04, 5→05, 10→06, 20→07, 50→08, 100→09, 125→0A, 200→0B Hz (1, 10 and 20 Hz confirmed on hardware; others verify) |
| wt_set_baud | FF AA 04 code 00 | 9600→02, 115200→06 |
| wt_calibrate_accel | FF AA 01 01 00, wait ~4 s, FF AA 01 00 00 | sensor flat and still, chip up |
| wt_zero_yaw | FF AA 01 04 00 | current heading becomes yaw 0 (confirmed on hardware) |
| wt_orientation | FF AA 23 00/01 00 | horizontal / vertical mounting |
| wt_factory_reset | FF AA 00 01 00 | back to defaults (9600 baud) |

## Procedures
- **Bring-up:** set the probe cube port to 9600 8N1 (`set_baud`, `set_data_bits`, `set_parity`, `set_stop_bits`). Then call `ports`, then `stream_probe`.
  Expect frames of all three types, checksum_errors 0 and a rate near the configured one.
- **Change the output rate:** `wt_set_rate`. Its result includes the measured rate. Warn the user before going above 20 Hz at 9600 baud.
- **Go to 115200:** first `wt_set_baud` with baud 115200 (the sensor switches and the stream stops), then `set_baud` 115200 on the probe cube port, then `stream_probe`.
  If frames do not come back, set the port back to 9600 and probe again. If that fails too, ask the user to power-cycle the sensor.
- **Calibrate** only when the user confirms the sensor is flat and still. Run it if the still test shows AX/AY means beyond ±0.1 g or Roll/Pitch beyond ±3° on a level surface.

## Probe cube quirk with a continuous stream
The probe cube firmware prints the console output with blocking calls. While the WT61PC streams, a long print such as the `ports` table
overruns the lab port's 16-byte FIFO, so each `ports` call can add ORE +1 and one damaged frame (checksum error or junk bytes).
This comes from the probe cube, not the sensor or the wiring. run_test avoids it (counters are cleared before and read after the measurement).
Only treat ORE or checksum errors as a link problem when they climb steadily, or appear in a test result.

## Failure signatures
- No frames at all, and rx not climbing: sensor TX wire, power (VCC/GND) or a wrong port.
- rx climbing but no valid frames, or many checksum errors / junk bytes: baud mismatch between the sensor and the port (the sensor may still be at 115200 or 9600 from an earlier session). Try the other supported baud.
- 0x00 floods or FE climbing: gross baud mismatch or the line held low.
- Frames arrive but the rate is far below the setting: the rate is too high for the baud.
- Gyro not near 0 while still: let it settle for a few seconds after power-up. If an offset stays, run accelerometer calibration on a level surface.
- Yaw creeping while still is normal (no magnetometer). Roll/pitch creeping is not.

## Axes
X points forward along the board, Y to the left, Z up out of the chip side. Roll is rotation about X, pitch about Y, yaw about Z.
Lying flat with the chip up reads AZ ≈ +1 g, Roll ≈ 0, Pitch ≈ 0.
