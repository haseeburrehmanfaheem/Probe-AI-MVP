/*
 * dummy_sensor.ino - fake UART sensor for the Nautilus UART LAB test bench
 *
 * Board  : Elegoo MEGA 2560 (ATmega2560)
 * Sensor : Serial1  -> pin 18 = TX1, pin 19 = RX1
 * Debug  : Serial   -> USB cable, 115200 (Arduino IDE Serial Monitor)
 *
 * Wiring to the STM32H723 (UART4):
 *   MEGA pin 18 (TX1) -> STM32 PD0 (UART4_RX)
 *   MEGA pin 19 (RX1) -> STM32 PD1 (UART4_TX)
 *   MEGA GND          -> STM32 GND          (mandatory!)
 *
 * Behaviour:
 *   - streams one fake reading per second:  $NAUT,T=24.83,H=57.20,SEQ=12*
 *   - answers line commands from the STM32:
 *       ID?         identity string
 *       READ        one reading on demand
 *       START/STOP  enable/disable the periodic stream
 *       RATE <ms>   stream period (min 100)
 *       PING        replies PONG
 *       ECHO <txt>  replies <txt>   (loopback integrity test)
 *   - everything sent/received is mirrored on the USB Serial Monitor.
 *
 * To exercise the STM32 'cfg' knobs, change SENSOR_CONFIG below and match it
 * on the bench, e.g. SERIAL_8E1 <-> 'cfg uart4 parity=e',
 *                    SERIAL_8N2 <-> 'cfg uart4 stop=2'.
 */

#define SENSOR_BAUD    9600
#define SENSOR_CONFIG  SERIAL_8N1

static bool          streaming      = true;
static unsigned long streamPeriodMs = 1000;
static unsigned long lastStream     = 0;
static unsigned long seq            = 0;

static char  rxLine[64];
static byte  rxLen = 0;

/* Slow fake temperature/humidity drift so consecutive values differ. */
static float fakeTemp()     { return 24.0 + 3.0 * sin(millis() / 60000.0 * TWO_PI); }
static float fakeHumidity() { return 55.0 + 8.0 * sin(millis() / 90000.0 * TWO_PI + 1.0); }

static void sendLine(const String &s)
{
  Serial1.print(s);
  Serial1.print("\r\n");
  Serial.print("[TX] ");        /* mirror on USB for eyeballing */
  Serial.println(s);
}

static void sendReading()
{
  String s = "$NAUT,T=" + String(fakeTemp(), 2) +
             ",H=" + String(fakeHumidity(), 2) +
             ",SEQ=" + String(seq++) + "*";
  sendLine(s);
}

static void handleCommand(char *cmd)
{
  Serial.print("[RX] ");
  Serial.println(cmd);

  if      (!strcmp(cmd, "ID?"))   sendLine("NAUTILUS-DUMMY-SENSOR v1 (MEGA2560)");
  else if (!strcmp(cmd, "READ"))  sendReading();
  else if (!strcmp(cmd, "START")) { streaming = true;  sendLine("OK STREAM ON");  }
  else if (!strcmp(cmd, "STOP"))  { streaming = false; sendLine("OK STREAM OFF"); }
  else if (!strcmp(cmd, "PING"))  sendLine("PONG");
  else if (!strncmp(cmd, "RATE ", 5)) {
    long ms = atol(cmd + 5);
    if (ms < 100) ms = 100;
    streamPeriodMs = (unsigned long)ms;
    sendLine("OK RATE " + String(streamPeriodMs));
  }
  else if (!strncmp(cmd, "ECHO ", 5)) sendLine(String(cmd + 5));
  else sendLine("ERR unknown cmd - ID? READ START STOP RATE PING ECHO");
}

void setup()
{
  Serial.begin(115200);
  Serial1.begin(SENSOR_BAUD, SENSOR_CONFIG);
  Serial.println("dummy sensor up: Serial1 @" + String(SENSOR_BAUD));
}

void loop()
{
  /* command input from the STM32, line-terminated by CR or LF */
  while (Serial1.available()) {
    char c = (char)Serial1.read();
    if (c == '\r' || c == '\n') {
      if (rxLen > 0) {
        rxLine[rxLen] = '\0';
        handleCommand(rxLine);
        rxLen = 0;
      }
    } else if (rxLen < sizeof(rxLine) - 1) {
      rxLine[rxLen++] = c;
    }
  }

  /* periodic fake reading */
  if (streaming && millis() - lastStream >= streamPeriodMs) {
    lastStream = millis();
    sendReading();
  }
}
