# NAUTILUS-DUMMY-SENSOR: notes for the assistant

- An Arduino MEGA 2560 running `nautilus_PTH_test/tools/dummy_sensor/dummy_sensor.ino` that pretends to be a temperature/humidity sensor on Serial1, fixed at 9600 8N1.
- Text protocol: every command is one line ending in CRLF. Replies: `ID?` → `NAUTILUS-DUMMY-SENSOR v1 (MEGA2560)`, `PING` → `PONG`, `READ` → one frame, `START`/`STOP` → `OK STREAM ON/OFF`, `RATE <ms>` → `OK RATE <ms>` (min 100), `ECHO <text>` → `<text>`, anything else → `ERR unknown cmd - ...`.
- Frames: `$NAUT,T=<degC>,H=<%RH>,SEQ=<n>*`. It streams at 1 Hz from power-up. SEQ goes up by 1 per frame, so a gap means a lost frame.
- The probe cube lab port boots at 115200, so until the baud is set to 9600 the port only sees a 0x00 flood (zeros and FE climb).
- The first sensor command after any port reconfiguration may get `ERR unknown cmd`. That reply is caused by stale bytes in the MEGA's line buffer; the backend retries once (`retried_after`).
- The sensor only supports 9600 8N1. Any other baud, parity or data bits breaks the link: the console accepts the setting, but the sensor stops answering.
