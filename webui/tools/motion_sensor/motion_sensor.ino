// Motion sensor for the Nautilus UART LAB: Arduino MEGA 2560 + a PIR module (e.g. HC-SR501) on pin 2.
//
// Speaks the same command set as tools/dummy_sensor/dummy_sensor.ino on Serial1 (TX1 pin 18 / RX1 pin 19,
// 9600 8N1), but streams motion frames instead of temperature/humidity:
//
//   $MOTN,L=<level 0.00-1.00>,M=<0|1>,SEQ=<n>*
//
// L is the PIR output smoothed into a 0-1 presence level, M is 1 while motion is detected
// (on above 0.5, off below 0.3). USB Serial at 115200 mirrors traffic as [TX]/[RX] lines.
//
// Wiring: PIR VCC -> 5V, PIR GND -> GND, PIR OUT -> pin 2. MEGA TX1 (18) -> PD0, RX1 (19) -> PD1, GND -> GND.

const uint8_t PIR_PIN = 2;
const long SENSOR_BAUD = 9600;
const unsigned long TICK_MS = 20;

unsigned long periodMs = 1000, lastFrame = 0, lastTick = 0, seq = 0;
bool streaming = true;   // streams from power-up, like the dummy sensor
float level = 0.05;
bool motion = false;
char line[64];
uint8_t len = 0;

void sendLine(const String &s) {
  Serial1.print(s); Serial1.print("\r\n");
  Serial.print("[TX] "); Serial.println(s);
}

String frame() {
  return "$MOTN,L=" + String(level, 2) + ",M=" + String(motion ? 1 : 0) + ",SEQ=" + String(seq++) + "*";
}

void updateLevel() {
  float target = digitalRead(PIR_PIN) == HIGH ? 0.9 : 0.05;
  level += (target - level) * 0.15;
  if (level > 0.5) motion = true;
  else if (level < 0.3) motion = false;
}

void handle(const char *cmd) {
  Serial.print("[RX] "); Serial.println(cmd);
  if (strcmp(cmd, "ID?") == 0) sendLine("NAUTILUS-MOTION-SENSOR v1 (MEGA2560)");
  else if (strcmp(cmd, "READ") == 0) sendLine(frame());
  else if (strcmp(cmd, "START") == 0) { streaming = true; sendLine("OK STREAM ON"); }
  else if (strcmp(cmd, "STOP") == 0) { streaming = false; sendLine("OK STREAM OFF"); }
  else if (strcmp(cmd, "PING") == 0) sendLine("PONG");
  else if (strncmp(cmd, "RATE ", 5) == 0) {
    long ms = atol(cmd + 5);
    periodMs = ms < 100 ? 100 : ms;
    sendLine("OK RATE " + String(periodMs));
  }
  else if (strncmp(cmd, "ECHO ", 5) == 0) sendLine(String(cmd + 5));
  else sendLine("ERR unknown cmd - ID? READ START STOP RATE PING ECHO");
}

void setup() {
  pinMode(PIR_PIN, INPUT);
  Serial.begin(115200);
  Serial1.begin(SENSOR_BAUD);
  Serial.println("NAUTILUS-MOTION-SENSOR ready");
}

void loop() {
  unsigned long now = millis();
  if (now - lastTick >= TICK_MS) { lastTick = now; updateLevel(); }
  if (streaming && now - lastFrame >= periodMs) { lastFrame = now; sendLine(frame()); }

  while (Serial1.available()) {
    char c = Serial1.read();
    if (c == '\r' || c == '\n') {
      if (len) { line[len] = '\0'; handle(line); len = 0; }
    } else if (len < sizeof(line) - 1) {
      line[len++] = c;
    }
  }
}
