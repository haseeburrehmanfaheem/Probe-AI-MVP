# NAUTILUS-MOTION-SENSOR: notes for the assistant

- An Arduino MEGA 2560 running `webui/tools/motion_sensor/motion_sensor.ino` with a PIR module (e.g. HC-SR501) on pin 2. It uses the same text command set as the dummy sensor (ID? PING READ START STOP RATE ECHO), at 9600 8N1 only.
- Frames: `$MOTN,L=<0.00-1.00>,M=<0|1>,SEQ=<n>*`. L is a smoothed presence level that rises toward ~0.85 while something moves and decays to ~0.05 when the scene is still. M is 1 while motion is detected (on above 0.5, off below 0.3).
- Tests need the user to physically move in front of the sensor (or press "Simulate motion" on the simulated bench) during the measurement.
- The first sensor command after a port reconfiguration may get `ERR unknown cmd` from stale bytes; the backend retries once.
