/**
  ******************************************************************************
  * @file    pth_sensor.c
  * @brief   PTH 4-20mA (temperature + humidity) transmitter driver read via a
  *          Waveshare "Modbus RTU Analog Input 8CH" module over RS485/Modbus.
  *
  *  Modbus link : USART2 (huart2) @ 9600 8N1, RS485 DE/RE = PE4 (RS485_DE_Pin)
  *  Debug/test  : USART3 (huart3) @ 115200 8N1 (ST-Link VCP)
  *
  *  Interactive test console: open the ST-Link VCP at 115200, send '?' + Enter.
  *
  *  NOTE: floats are formatted with integer math (f2s) so the debug output
  *  works WITHOUT enabling "-u _printf_float" in the linker.
  ******************************************************************************
  */
#include "pth_sensor.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>

/* ==========================================================================
 *  Modbus / Waveshare protocol constants
 * ========================================================================== */
#define MB_FUNC_READ_HOLDING     0x03
#define MB_FUNC_READ_INPUT       0x04
#define MB_FUNC_WRITE_SINGLE     0x06

/* Waveshare 8CH: analog acquisition values live in INPUT registers 0..7.
 * Per-channel data-type config lives in holding registers based at 0x1000
 * (0-5V=0, 0-10V=1, 0-20mA=2, 4-20mA=3, raw=4). Writing it is OPTIONAL and
 * only helps if your module supports software range selection.             */
#define WS_INPUT_REG_BASE        0x0000
#define WS_CFG_REG_BASE          0x1000
#define WS_DATATYPE_4_20MA       0x0003

#define MB_TX_LEN                8      /* addr+func+start(2)+qty(2)+crc(2)   */
#define MB_RX_MAX                32
#define MB_RX_TIMEOUT_MS         200    /* per-transaction response timeout   */
#define RS485_TX_SETTLE_MS       1      /* DE assert -> first bit             */

#define RS485_TX()  HAL_GPIO_WritePin(RS485_DE_GPIO_Port, RS485_DE_Pin, GPIO_PIN_SET)
#define RS485_RX()  HAL_GPIO_WritePin(RS485_DE_GPIO_Port, RS485_DE_Pin, GPIO_PIN_RESET)

/* ==========================================================================
 *  Runtime state (all console-adjustable)
 * ========================================================================== */
static UART_HandleTypeDef *g_mb  = NULL;   /* Modbus UART                     */
static UART_HandleTypeDef *g_dbg = NULL;   /* console UART                    */

static uint8_t        g_addr       = PTH_DEFAULT_SLAVE_ADDR;
static uint8_t        g_hum_ch     = PTH_DEFAULT_HUM_CHANNEL;
static uint8_t        g_temp_ch    = PTH_DEFAULT_TEMP_CHANNEL;
static float          g_temp_min   = PTH_DEFAULT_TEMP_MIN_C;
static float          g_temp_max   = PTH_DEFAULT_TEMP_MAX_C;
static float          g_hum_min    = PTH_DEFAULT_HUM_MIN_PCT;
static float          g_hum_max    = PTH_DEFAULT_HUM_MAX_PCT;
static PTH_ScaleMode  g_scale      = PTH_DEFAULT_SCALE_MODE;
static uint8_t        g_verbose    = 1;    /* 0=quiet 1=normal 2=frame dumps  */
static bool           g_continuous = false;
static uint32_t       g_poll_ms    = PTH_DEFAULT_POLL_MS;
static uint32_t       g_last_poll  = 0;

/* Last raw Modbus frames, kept for the 'dump' command / verbose level 2.     */
static uint8_t  g_last_tx[MB_TX_LEN];
static uint8_t  g_last_rx[MB_RX_MAX];
static uint16_t g_last_rx_len = 0;

/* ==========================================================================
 *  Float -> string (integer math; no float-printf dependency)
 *  e.g. f2s(-12.345f, 2, buf) -> "-12.35"
 * ========================================================================== */
static const char *f2s(float v, int dec, char *buf, size_t n)
{
    if (dec < 0) dec = 0;
    if (dec > 4) dec = 4;
    int neg = (v < 0.0f);
    if (neg) v = -v;

    long scale = 1;
    for (int i = 0; i < dec; i++) scale *= 10;

    long scaled = (long)(v * (float)scale + 0.5f);   /* round                */
    long ip = scaled / scale;
    long fp = scaled % scale;

    if (dec > 0) snprintf(buf, n, "%s%ld.%0*ld", neg ? "-" : "", ip, dec, fp);
    else         snprintf(buf, n, "%s%ld",        neg ? "-" : "", ip);
    return buf;
}

/* ==========================================================================
 *  Debug print helpers (console = huart3). Format strings use NO %f.
 * ========================================================================== */
static void dbg(const char *s)
{
    if (g_dbg) HAL_UART_Transmit(g_dbg, (uint8_t *)s, (uint16_t)strlen(s), 300);
}

static void dbgf(const char *fmt, ...)
{
    char buf[192];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    dbg(buf);
}

static void dbg_hex(const char *tag, const uint8_t *p, uint16_t nbytes)
{
    dbgf("%s[%u]:", tag, nbytes);
    for (uint16_t i = 0; i < nbytes; i++) dbgf(" %02X", p[i]);
    dbg("\r\n");
}

/* ==========================================================================
 *  Modbus RTU core
 * ========================================================================== */
static uint16_t modbus_crc16(const uint8_t *buf, uint16_t len)
{
    uint16_t crc = 0xFFFF;
    for (uint16_t pos = 0; pos < len; pos++) {
        crc ^= (uint16_t)buf[pos];
        for (uint8_t i = 8; i != 0; i--) {
            if (crc & 0x0001) { crc >>= 1; crc ^= 0xA001; }
            else              { crc >>= 1; }
        }
    }
    return crc;
}

/* Drain any stale bytes sitting in the UART RX before a new transaction.     */
static void modbus_flush_rx(void)
{
    uint8_t junk;
    while (HAL_UART_Receive(g_mb, &junk, 1, 0) == HAL_OK) { /* discard */ }
    __HAL_UART_CLEAR_OREFLAG(g_mb);
}

/**
 * Read @p qty registers of type @p func starting at @p start_reg from the
 * current slave. Decoded 16-bit values written big-endian into @p out.
 * Fills g_last_tx / g_last_rx for diagnostics.
 */
static PTH_Status modbus_read(uint8_t func, uint16_t start_reg,
                              uint16_t qty, uint16_t *out)
{
    uint8_t tx[MB_TX_LEN];
    tx[0] = g_addr;
    tx[1] = func;
    tx[2] = (uint8_t)(start_reg >> 8);
    tx[3] = (uint8_t)(start_reg & 0xFF);
    tx[4] = (uint8_t)(qty >> 8);
    tx[5] = (uint8_t)(qty & 0xFF);
    uint16_t crc = modbus_crc16(tx, 6);
    tx[6] = (uint8_t)(crc & 0xFF);
    tx[7] = (uint8_t)(crc >> 8);
    memcpy(g_last_tx, tx, MB_TX_LEN);

    modbus_flush_rx();

    /* ---- transmit (half-duplex: drive DE, wait for TC, release DE) ---- */
    RS485_TX();
    HAL_Delay(RS485_TX_SETTLE_MS);
    HAL_UART_Transmit(g_mb, tx, MB_TX_LEN, 100);
    while (__HAL_UART_GET_FLAG(g_mb, UART_FLAG_TC) == RESET) { /* wait TC */ }
    RS485_RX();

    /* ---- receive: addr(1)+func(1)+bytecount(1)+data(2*qty)+crc(2) ---- */
    uint16_t expected = 5 + qty * 2;
    if (expected > MB_RX_MAX) expected = MB_RX_MAX;
    memset(g_last_rx, 0, sizeof(g_last_rx));
    g_last_rx_len = 0;

    if (HAL_UART_Receive(g_mb, g_last_rx, expected, MB_RX_TIMEOUT_MS) != HAL_OK)
        return PTH_ERR_TIMEOUT;
    g_last_rx_len = expected;

    if (g_last_rx[0] != g_addr)         return PTH_ERR_ADDR;
    if (g_last_rx[1] == (func | 0x80))  return PTH_ERR_EXCEPTION;
    if (g_last_rx[1] != func)           return PTH_ERR_FUNC;

    uint16_t rx_crc = (uint16_t)g_last_rx[expected - 2]
                    | ((uint16_t)g_last_rx[expected - 1] << 8);
    if (rx_crc != modbus_crc16(g_last_rx, expected - 2)) return PTH_ERR_CRC;

    for (uint16_t i = 0; i < qty; i++)
        out[i] = ((uint16_t)g_last_rx[3 + i * 2] << 8) | g_last_rx[4 + i * 2];

    return PTH_OK;
}

/* Optional: push a 4-20mA data-type config to one channel (module dependent) */
static PTH_Status modbus_write_single(uint16_t reg, uint16_t val)
{
    uint8_t tx[MB_TX_LEN];
    tx[0] = g_addr;
    tx[1] = MB_FUNC_WRITE_SINGLE;
    tx[2] = (uint8_t)(reg >> 8);
    tx[3] = (uint8_t)(reg & 0xFF);
    tx[4] = (uint8_t)(val >> 8);
    tx[5] = (uint8_t)(val & 0xFF);
    uint16_t crc = modbus_crc16(tx, 6);
    tx[6] = (uint8_t)(crc & 0xFF);
    tx[7] = (uint8_t)(crc >> 8);
    memcpy(g_last_tx, tx, MB_TX_LEN);

    modbus_flush_rx();
    RS485_TX();
    HAL_Delay(RS485_TX_SETTLE_MS);
    HAL_UART_Transmit(g_mb, tx, MB_TX_LEN, 100);
    while (__HAL_UART_GET_FLAG(g_mb, UART_FLAG_TC) == RESET) { }
    RS485_RX();

    uint8_t rx[8];
    if (HAL_UART_Receive(g_mb, rx, 8, MB_RX_TIMEOUT_MS) != HAL_OK) return PTH_ERR_TIMEOUT;
    return (rx[0] == g_addr && rx[1] == MB_FUNC_WRITE_SINGLE) ? PTH_OK : PTH_ERR_FUNC;
}

/* ==========================================================================
 *  Signal conversion : raw register -> mA -> engineering units
 * ========================================================================== */
static float raw_to_mA(uint16_t raw)
{
    if (g_scale == PTH_SCALE_UA)
        return (float)raw / 1000.0f;                 /* micro-amps -> mA      */
    return ((float)raw / PTH_RAW_FULLSCALE_COUNTS) * PTH_RAW_FULLSCALE_MA;
}

static float mA_to_eng(float mA, float lo, float hi)
{
    return lo + (mA - 4.0f) / 16.0f * (hi - lo);     /* linear 4..20mA span   */
}

static PTH_Status decode_channel(uint16_t raw, float lo, float hi,
                                 float *mA_out, float *eng_out)
{
    float mA = raw_to_mA(raw);
    *mA_out  = mA;
    *eng_out = mA_to_eng(mA, lo, hi);
    if (mA < PTH_MA_UNDERRANGE) return PTH_ERR_UNDERRANGE;
    if (mA > PTH_MA_OVERRANGE)  return PTH_ERR_OVERRANGE;
    return PTH_OK;
}

/* ==========================================================================
 *  Public read
 * ========================================================================== */
PTH_Status PTH_ReadAll(PTH_Reading *r)
{
    memset(r, 0, sizeof(*r));
    uint16_t v = 0;
    PTH_Status comms;

    /* Humidity channel */
    comms = modbus_read(MB_FUNC_READ_INPUT, WS_INPUT_REG_BASE + g_hum_ch, 1, &v);
    if (comms != PTH_OK) {
        r->hum_status = comms; r->hum_valid = false;
    } else {
        r->raw_hum = v;
        r->hum_status = decode_channel(v, g_hum_min, g_hum_max, &r->hum_mA, &r->humidity);
        r->hum_valid = (r->hum_status == PTH_OK);
    }

    /* Temperature channel */
    comms = modbus_read(MB_FUNC_READ_INPUT, WS_INPUT_REG_BASE + g_temp_ch, 1, &v);
    if (comms != PTH_OK) {
        r->temp_status = comms; r->temp_valid = false;
    } else {
        r->raw_temp = v;
        r->temp_status = decode_channel(v, g_temp_min, g_temp_max, &r->temp_mA, &r->temperature);
        r->temp_valid = (r->temp_status == PTH_OK);
    }

    if (!r->hum_valid || !r->temp_valid)
        return (r->hum_status != PTH_OK) ? r->hum_status : r->temp_status;
    return PTH_OK;
}

const char *PTH_StatusStr(PTH_Status s)
{
    switch (s) {
        case PTH_OK:             return "OK";
        case PTH_ERR_TIMEOUT:    return "TIMEOUT (no reply - check wiring/addr/baud)";
        case PTH_ERR_CRC:        return "CRC error (noise/termination)";
        case PTH_ERR_ADDR:       return "wrong slave addr in reply";
        case PTH_ERR_FUNC:       return "unexpected function code";
        case PTH_ERR_EXCEPTION:  return "Modbus exception";
        case PTH_ERR_UNDERRANGE: return "UNDER-RANGE (<3.5mA: open loop / sensor unpowered)";
        case PTH_ERR_OVERRANGE:  return "OVER-RANGE (>21mA: short / miswire)";
        default:                 return "?";
    }
}

/* ==========================================================================
 *  Detailed reading print + activity LEDs
 * ========================================================================== */
static bool is_comms_fail(PTH_Status s)
{
    return (s == PTH_ERR_TIMEOUT || s == PTH_ERR_CRC || s == PTH_ERR_ADDR ||
            s == PTH_ERR_FUNC    || s == PTH_ERR_EXCEPTION);
}

void PTH_PrintReading(const PTH_Reading *r)
{
    char maBuf[16], engBuf[16], spanBuf[16];

    dbgf("\r\n---- PTH read @ %lu ms  (slave 0x%02X) ----\r\n",
         (unsigned long)HAL_GetTick(), g_addr);

    /* Humidity */
    if (is_comms_fail(r->hum_status)) {
        dbgf("  HUM  (ch%u)  COMMS FAIL: %s\r\n", g_hum_ch, PTH_StatusStr(r->hum_status));
    } else {
        f2s(r->hum_mA, 3, maBuf, sizeof(maBuf));
        f2s(r->humidity, 2, engBuf, sizeof(engBuf));
        f2s((r->hum_mA - 4.0f) / 16.0f * 100.0f, 1, spanBuf, sizeof(spanBuf));
        dbgf("  HUM  (ch%u)  raw=0x%04X (%5u)  %s mA  span=%s%%  => %s %%RH   [%s]\r\n",
             g_hum_ch, r->raw_hum, r->raw_hum, maBuf, spanBuf, engBuf,
             PTH_StatusStr(r->hum_status));
    }

    /* Temperature */
    if (is_comms_fail(r->temp_status)) {
        dbgf("  TEMP (ch%u)  COMMS FAIL: %s\r\n", g_temp_ch, PTH_StatusStr(r->temp_status));
    } else {
        f2s(r->temp_mA, 3, maBuf, sizeof(maBuf));
        f2s(r->temperature, 2, engBuf, sizeof(engBuf));
        f2s((r->temp_mA - 4.0f) / 16.0f * 100.0f, 1, spanBuf, sizeof(spanBuf));
        dbgf("  TEMP (ch%u)  raw=0x%04X (%5u)  %s mA  span=%s%%  => %s degC  [%s]\r\n",
             g_temp_ch, r->raw_temp, r->raw_temp, maBuf, spanBuf, engBuf,
             PTH_StatusStr(r->temp_status));
    }

    if (g_verbose >= 2) {
        dbg_hex("  TX", g_last_tx, MB_TX_LEN);
        if (g_last_rx_len) dbg_hex("  RX", g_last_rx, g_last_rx_len);
    }

    /* Activity LEDs: green = both good, red = any fault */
    if (r->hum_valid && r->temp_valid) {
        HAL_GPIO_WritePin(LED_GREEN_GPIO_Port, LED_GREEN_Pin, GPIO_PIN_SET);
        HAL_GPIO_WritePin(LED_RED_GPIO_Port,   LED_RED_Pin,   GPIO_PIN_RESET);
    } else {
        HAL_GPIO_WritePin(LED_GREEN_GPIO_Port, LED_GREEN_Pin, GPIO_PIN_RESET);
        HAL_GPIO_WritePin(LED_RED_GPIO_Port,   LED_RED_Pin,   GPIO_PIN_SET);
    }
    HAL_GPIO_TogglePin(STATUS_LED_GPIO_Port, STATUS_LED_Pin);
}

/* ==========================================================================
 *  Interactive test console  (USART3 / VCP @ 115200)
 * ========================================================================== */
static void print_help(void)
{
    dbg("\r\n======================= PTH TEST CONSOLE =======================\r\n");
    dbg(" read            one detailed reading of both channels\r\n");
    dbg(" cont / stop     start/stop continuous polling\r\n");
    dbg(" scan            dump raw values of ALL 8 Waveshare channels\r\n");
    dbg(" dump            show last Modbus TX/RX frames (hex)\r\n");
    dbg(" cfg             write 4-20mA data-type to both channels (optional)\r\n");
    dbg(" addr <n>        set Modbus slave address (1..247)\r\n");
    dbg(" ch <hum> <tmp>  set humidity & temperature channel regs (0..7)\r\n");
    dbg(" temp <lo> <hi>  set temperature range for 4-20mA (degC)\r\n");
    dbg(" hum  <lo> <hi>  set humidity range for 4-20mA (%RH)\r\n");
    dbg(" mode ua | raw   raw register interpretation (uA vs 0-4095 counts)\r\n");
    dbg(" rate <ms>       continuous poll period\r\n");
    dbg(" verbose <0-2>   0 quiet, 1 normal, 2 + frame hex dumps\r\n");
    dbg(" cfgshow         print current configuration\r\n");
    dbg(" ?               this help\r\n");
    dbg("================================================================\r\n");
}

static void print_cfg(void)
{
    char a[16], b[16], c[16], d[16];
    dbgf("\r\n cfg: addr=0x%02X  hum_ch=%u  temp_ch=%u\r\n", g_addr, g_hum_ch, g_temp_ch);
    dbgf("      temp range = %s .. %s degC\r\n",
         f2s(g_temp_min, 1, a, sizeof(a)), f2s(g_temp_max, 1, b, sizeof(b)));
    dbgf("      hum  range = %s .. %s %%RH\r\n",
         f2s(g_hum_min, 1, c, sizeof(c)), f2s(g_hum_max, 1, d, sizeof(d)));
    dbgf("      scale=%s  verbose=%u  poll=%lu ms  continuous=%s\r\n",
         (g_scale == PTH_SCALE_UA) ? "uA" : "raw",
         g_verbose, (unsigned long)g_poll_ms, g_continuous ? "ON" : "OFF");
}

static void cmd_scan(void)
{
    char mbuf[16];
    dbg("\r\n-- scanning all 8 channels (input regs 0..7) --\r\n");
    for (uint8_t ch = 0; ch < 8; ch++) {
        uint16_t v = 0;
        PTH_Status s = modbus_read(MB_FUNC_READ_INPUT, WS_INPUT_REG_BASE + ch, 1, &v);
        if (s == PTH_OK)
            dbgf("  ch%u  raw=0x%04X (%5u)  ~%s mA\r\n",
                 ch, v, v, f2s(raw_to_mA(v), 3, mbuf, sizeof(mbuf)));
        else
            dbgf("  ch%u  %s\r\n", ch, PTH_StatusStr(s));
        HAL_Delay(20);
    }
}

static void cmd_cfg_write(void)
{
    dbg("\r\n-- writing 4-20mA data-type to hum & temp channels --\r\n");
    PTH_Status s1 = modbus_write_single(WS_CFG_REG_BASE + g_hum_ch,  WS_DATATYPE_4_20MA);
    dbgf("  hum  ch%u cfg: %s\r\n", g_hum_ch, PTH_StatusStr(s1));
    HAL_Delay(20);
    PTH_Status s2 = modbus_write_single(WS_CFG_REG_BASE + g_temp_ch, WS_DATATYPE_4_20MA);
    dbgf("  temp ch%u cfg: %s\r\n", g_temp_ch, PTH_StatusStr(s2));
    dbg("  (ignore failures if your module selects range by jumper - harmless)\r\n");
}

/* Split a command line into tokens (in place). Returns token count.          */
static int tokenize(char *line, char *tok[], int max)
{
    int n = 0;
    char *p = line;
    while (*p && n < max) {
        while (*p == ' ' || *p == '\t') p++;
        if (!*p) break;
        tok[n++] = p;
        while (*p && *p != ' ' && *p != '\t') p++;
        if (*p) *p++ = '\0';
    }
    return n;
}

static void handle_command(char *line)
{
    char *tok[6];
    int n = tokenize(line, tok, 6);
    if (n == 0) return;

    if      (!strcmp(tok[0], "?") || !strcmp(tok[0], "help")) print_help();
    else if (!strcmp(tok[0], "cfgshow")) print_cfg();
    else if (!strcmp(tok[0], "read")) { PTH_Reading r; PTH_ReadAll(&r); PTH_PrintReading(&r); }
    else if (!strcmp(tok[0], "cont")) { g_continuous = true;  g_last_poll = 0; dbg("continuous ON\r\n"); }
    else if (!strcmp(tok[0], "stop")) { g_continuous = false; dbg("continuous OFF\r\n"); }
    else if (!strcmp(tok[0], "scan")) cmd_scan();
    else if (!strcmp(tok[0], "cfg"))  cmd_cfg_write();
    else if (!strcmp(tok[0], "dump")) {
        dbg_hex("last TX", g_last_tx, MB_TX_LEN);
        if (g_last_rx_len) dbg_hex("last RX", g_last_rx, g_last_rx_len);
        else dbg("last RX: (none)\r\n");
    }
    else if (!strcmp(tok[0], "addr") && n >= 2) {
        int a = atoi(tok[1]);
        if (a >= 1 && a <= 247) { g_addr = (uint8_t)a; dbgf("slave addr = 0x%02X\r\n", g_addr); }
        else dbg("addr must be 1..247\r\n");
    }
    else if (!strcmp(tok[0], "ch") && n >= 3) {
        int h = atoi(tok[1]), t = atoi(tok[2]);
        if (h >= 0 && h <= 7 && t >= 0 && t <= 7) {
            g_hum_ch = (uint8_t)h; g_temp_ch = (uint8_t)t;
            dbgf("hum_ch=%u temp_ch=%u\r\n", g_hum_ch, g_temp_ch);
        } else dbg("channels must be 0..7\r\n");
    }
    else if (!strcmp(tok[0], "temp") && n >= 3) {
        char a[16], b[16];
        g_temp_min = (float)atof(tok[1]); g_temp_max = (float)atof(tok[2]);
        dbgf("temp range = %s .. %s degC\r\n",
             f2s(g_temp_min, 1, a, sizeof(a)), f2s(g_temp_max, 1, b, sizeof(b)));
    }
    else if (!strcmp(tok[0], "hum") && n >= 3) {
        char a[16], b[16];
        g_hum_min = (float)atof(tok[1]); g_hum_max = (float)atof(tok[2]);
        dbgf("hum range = %s .. %s %%RH\r\n",
             f2s(g_hum_min, 1, a, sizeof(a)), f2s(g_hum_max, 1, b, sizeof(b)));
    }
    else if (!strcmp(tok[0], "mode") && n >= 2) {
        if      (!strcmp(tok[1], "ua"))  { g_scale = PTH_SCALE_UA;  dbg("scale = uA\r\n"); }
        else if (!strcmp(tok[1], "raw")) { g_scale = PTH_SCALE_RAW; dbg("scale = raw\r\n"); }
        else dbg("mode ua | raw\r\n");
    }
    else if (!strcmp(tok[0], "rate") && n >= 2) {
        int ms = atoi(tok[1]); if (ms < 50) ms = 50;
        g_poll_ms = (uint32_t)ms; dbgf("poll = %lu ms\r\n", (unsigned long)g_poll_ms);
    }
    else if (!strcmp(tok[0], "verbose") && n >= 2) {
        int v = atoi(tok[1]); if (v < 0) v = 0; if (v > 2) v = 2;
        g_verbose = (uint8_t)v; dbgf("verbose = %u\r\n", g_verbose);
    }
    else dbg("unknown command - type ? for help\r\n");

    dbg("> ");
}

/* Non-blocking single-char console reader -> line buffer.                    */
static void service_console(void)
{
    static char line[64];
    static uint8_t idx = 0;

    if (!g_dbg) return;
    while (__HAL_UART_GET_FLAG(g_dbg, UART_FLAG_RXNE) != RESET) {
        uint8_t c = (uint8_t)(g_dbg->Instance->RDR & 0xFF);
        if (c == '\r' || c == '\n') {
            if (idx > 0) {
                line[idx] = '\0';
                dbg("\r\n");
                handle_command(line);
                idx = 0;
            }
        } else if (c == 0x08 || c == 0x7F) {          /* backspace           */
            if (idx > 0) { idx--; dbg("\b \b"); }
        } else if (idx < sizeof(line) - 1) {
            line[idx++] = (char)c;
            HAL_UART_Transmit(g_dbg, &c, 1, 10);      /* echo                */
        }
    }
}

/* ==========================================================================
 *  Lifecycle
 * ========================================================================== */
void PTH_Init(UART_HandleTypeDef *modbus_uart, UART_HandleTypeDef *debug_uart)
{
    g_mb  = modbus_uart;
    g_dbg = debug_uart;
    RS485_RX();                      /* default to listen                    */

    dbg("\r\n\r\n############################################################\r\n");
    dbg("  Nautilus PTH sensor test  (Waveshare 8CH Modbus / RS485)\r\n");
    dbg("  Modbus: USART2 @9600 8N1  DE=PE4   Console: USART3 @115200\r\n");
    dbg("############################################################\r\n");
    print_cfg();
    print_help();
    dbg("\r\nTip: send 'read' for one shot, or 'cont' to stream.\r\n> ");
}

void PTH_Task(void)
{
    service_console();

    if (g_continuous) {
        uint32_t now = HAL_GetTick();
        if ((now - g_last_poll) >= g_poll_ms) {
            g_last_poll = now;
            PTH_Reading r;
            PTH_ReadAll(&r);
            if (g_verbose >= 1) PTH_PrintReading(&r);
        }
    }
}
