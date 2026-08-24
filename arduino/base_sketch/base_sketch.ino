#include <SoftwareSerial.h>

SoftwareSerial sensorSerial(10, 11);
byte packet[11];
int packetIndex = 0;
unsigned long measurementStart = 0;
unsigned long accelPacketCount = 0;
float measuredHz = 0.0;

void sendCommand(byte a, byte b, byte c, byte d, byte e) {
  sensorSerial.write(a);
  sensorSerial.write(b);
  sensorSerial.write(c);
  sensorSerial.write(d);
  sensorSerial.write(e);
  delay(100);
}

bool getRateValue(int hz, byte &rateValue) {
  switch (hz) {
    case 1:  rateValue = 0x03; return true;
    case 2:  rateValue = 0x04; return true;
    case 5:  rateValue = 0x05; return true;
    case 10: rateValue = 0x06; return true;
    case 20: rateValue = 0x07; return true;
    default: return false;
  }
}

void resetMeasurement() {
  accelPacketCount = 0;
  measurementStart = millis();
}

bool setOutputRate(int hz) {
  byte rateValue;
  if (!getRateValue(hz, rateValue)) {
    Serial.print("ERR,INVALID_RATE,");
    Serial.println(hz);
    return false;
  }
  sendCommand(0xFF, 0xAA, 0x69, 0x88, 0xB5);
  sendCommand(0xFF, 0xAA, 0x03, rateValue, 0x00);
  sendCommand(0xFF, 0xAA, 0x00, 0x00, 0x00);
  resetMeasurement();
  Serial.print("ACK,SET_RATE,");
  Serial.println(hz);
  return true;
}

bool validChecksum() {
  byte checksum = 0;
  for (int i = 0; i < 10; i++) checksum += packet[i];
  return checksum == packet[10];
}

int16_t toInt16(byte lo, byte hi) {
  return (int16_t)((uint16_t)hi << 8 | lo);
}

void printScaledValue(const char *label, int16_t raw, float scale, const char *unit) {
  Serial.print(label);
  Serial.print(raw * scale, 2);
  Serial.print(unit);
}

void processPacket() {
  if (!validChecksum()) return;

  // WT61PC accelerometer packet
  if (packet[1] == 0x51) {
    accelPacketCount++;
    int16_t ax = toInt16(packet[2], packet[3]);
    int16_t ay = toInt16(packet[4], packet[5]);
    int16_t az = toInt16(packet[6], packet[7]);

    // Human-readable acceleration in g, using common WT61PC scaling (16-bit / 32768 * 16g)
    Serial.print("ACCEL,g,AX=");
    Serial.print(ax * 16.0 / 32768.0, 3);
    Serial.print(",AY=");
    Serial.print(ay * 16.0 / 32768.0, 3);
    Serial.print(",AZ=");
    Serial.println(az * 16.0 / 32768.0, 3);
    return;
  }

  // WT61PC gyroscope packet
  if (packet[1] == 0x52) {
    int16_t gx = toInt16(packet[2], packet[3]);
    int16_t gy = toInt16(packet[4], packet[5]);
    int16_t gz = toInt16(packet[6], packet[7]);

    // Human-readable angular rate in deg/s, using common WT61PC scaling (16-bit / 32768 * 2000 dps)
    Serial.print("GYRO,dps,GX=");
    Serial.print(gx * 2000.0 / 32768.0, 3);
    Serial.print(",GY=");
    Serial.print(gy * 2000.0 / 32768.0, 3);
    Serial.print(",GZ=");
    Serial.println(gz * 2000.0 / 32768.0, 3);
    return;
  }
}

void readSensor() {
  while (sensorSerial.available()) {
    byte b = sensorSerial.read();
    if (packetIndex == 0 && b != 0x55) continue;
    packet[packetIndex++] = b;
    if (packetIndex == 11) {
      processPacket();
      packetIndex = 0;
    }
  }
}

void updateFrequencyMeasurement() {
  unsigned long elapsed = millis() - measurementStart;
  if (elapsed >= 5000) {
    measuredHz = accelPacketCount / (elapsed / 1000.0);
    Serial.print("FREQ,");
    Serial.println(measuredHz, 2);
    resetMeasurement();
  }
}

void handleSerialCommands() {
  if (!Serial.available()) return;
  String command = Serial.readStringUntil('\n');
  command.trim();
  if (command.startsWith("SET_RATE,")) {
    setOutputRate(command.substring(9).toInt());
    return;
  }
  if (command == "MEASURE") {
    resetMeasurement();
    Serial.println("ACK,MEASURE");
    return;
  }
  if (command == "GET_FREQ") {
    Serial.print("FREQ,");
    Serial.println(measuredHz, 2);
    return;
  }
  Serial.print("ERR,UNKNOWN_COMMAND,");
  Serial.println(command);
}

void setup() {
  Serial.begin(115200);
  sensorSerial.begin(9600);
  Serial.setTimeout(100);
  delay(1000);
  Serial.println("READY,WT61PC");
  resetMeasurement();
}

void loop() {
  readSensor();
  updateFrequencyMeasurement();
  handleSerialCommands();
}