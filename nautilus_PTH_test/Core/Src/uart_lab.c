/**
  ******************************************************************************
  * @file    uart_lab.c
  * @brief   Generic runtime-configurable UART test bench.
  *
  *  Console : USART3 (ST-Link VCP) @ 115200 8N1 - send '?' for help.
  *  Ports   : uart4  (PD1 TX / PD0 RX,  TTL)
  *            usart2 (PD5 TX / PD6 RX,  RS485 transceiver, DE = PE4)
  *            usart6 (PC6 TX / PC7 RX,  TTL)
  *
  *  Everything a device profile needs is settable at runtime per port:
  *    baud, data bits (7/8/9), parity, stop bits (0.5/1/1.5/2),
  *    UART clock prescaler (/1../256), kernel clock source (pclk/hsi/csi/lse),
  *    FIFO mode, TX/RX pin swap, RS485 DE direction control.
  *
  *  KERNEL CLOCK CAVEAT (H7): USART2/3 + UART4/5/7/8 share ONE kernel-clock
  *  mux, USART1/6/9/10 share another. Changing 'clk' on uart4 or usart2 also
  *  moves the console's (USART3) kernel clock - this module transparently
  *  re-inits the console so it stays at 115200. 'clk=lse' is therefore only
  *  allowed on usart6 (32.768 kHz could never carry the 115200 console).
  ******************************************************************************
  */
#include "uart_lab.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>

extern UART_HandleTypeDef huart2;
extern UART_HandleTypeDef huart4;
extern UART_HandleTypeDef huart6;

/* ==========================================================================
 *  Types / tables
 * ========================================================================== */
typedef enum { KCLK_PCLK = 0, KCLK_HSI, KCLK_CSI, KCLK_LSE } KClkSel;
static const char *KCLK_NAMES[] = { "pclk", "hsi", "csi", "lse" };

typedef struct { uint16_t div; uint32_t macro; } PrescEntry;
static const PrescEntry PRESC_TAB[] = {
    {   1, UART_PRESCALER_DIV1   }, {   2, UART_PRESCALER_DIV2   },
    {   4, UART_PRESCALER_DIV4   }, {   6, UART_PRESCALER_DIV6   },
    {   8, UART_PRESCALER_DIV8   }, {  10, UART_PRESCALER_DIV10  },
    {  12, UART_PRESCALER_DIV12  }, {  16, UART_PRESCALER_DIV16  },
    {  32, UART_PRESCALER_DIV32  }, {  64, UART_PRESCALER_DIV64  },
    { 128, UART_PRESCALER_DIV128 }, { 256, UART_PRESCALER_DIV256 },
};
#define PRESC_COUNT  (sizeof(PRESC_TAB) / sizeof(PRESC_TAB[0]))

#define RBUF_LEN        96      /* per-port RX capture buffer                */
#define MON_FLUSH_MS    10      /* idle gap that ends a monitored RX burst   */
#define MON_FLUSH_LEN   64      /* ...or flush once this many bytes queued   */

typedef struct {
    const char         *name;
    UART_HandleTypeDef *huart;
    GPIO_TypeDef       *de_port;    /* RS485 driver-enable pin (NULL = none) */
    uint16_t            de_pin;
    GPIO_TypeDef       *rx_gpio;    /* RX pin, re-inited with a pull-up so an
                                     * unconnected port idles high + silent  */
    uint16_t            rx_pin;
    uint8_t             rx_af;
    const char         *boot_profile;  /* device profile bound at startup, or
                                        * NULL to come up with the raw
                                        * CubeMX framing below              */

    /* desired configuration - applied to hardware by port_apply() */
    uint32_t  baud;
    uint8_t   databits;             /* payload bits excl. parity: 7, 8, 9    */
    char      parity;               /* 'n' / 'e' / 'o'                       */
    uint32_t  stopbits;             /* UART_STOPBITS_x                       */
    uint8_t   presc_idx;            /* index into PRESC_TAB                  */
    KClkSel   kclk;
    bool      fifo;
    bool      swap;
    bool      rs485;                /* wrap TX in DE assert/release          */

    /* runtime state */
    bool      open;
    bool      monitor;
    uint32_t  rx_bytes, tx_bytes;
    uint32_t  err_ore, err_fe, err_ne, err_pe;
    uint32_t  zeros_supp;           /* all-0x00 junk bursts held back        */
    uint32_t  mon_dropped;          /* bytes discarded while user was typing */
    uint32_t  last_zero_report;
    uint8_t   rbuf[RBUF_LEN];
    uint16_t  rlen;
    uint32_t  last_rx;
    uint8_t   lastfrm[RBUF_LEN];    /* most recent burst, for 'peek'         */
    uint16_t  lastfrm_len;
    uint32_t  frames;               /* bursts received since boot/clear      */
} LabPort;

/* The framing fields mirror the CubeMX MX_*_Init values, so a port with no
 * boot_profile comes up exactly as the generated init left it. Where a known
 * device is wired to a port, name its profile in boot_profile instead of
 * hand-editing the framing here: the profile stays the single source of truth
 * and the port is already configured at the prompt, with no 'bind' to type.  */
static LabPort g_ports[] = {
    { .name = "uart4",  .huart = &huart4, .de_port = NULL,
      .rx_gpio = GPIOD, .rx_pin = GPIO_PIN_0, .rx_af = GPIO_AF8_UART4,
      .boot_profile = "uno-accel",      /* Arduino UNO accelerometer @9600   */
      .baud = 115200, .databits = 8, .parity = 'n', .stopbits = UART_STOPBITS_1,
      .presc_idx = 0, .kclk = KCLK_PCLK, .fifo = true, .monitor = true },
    { .name = "usart2", .huart = &huart2,
      .de_port = RS485_DE_GPIO_Port, .de_pin = RS485_DE_Pin, .rs485 = true,
      .rx_gpio = GPIOD, .rx_pin = GPIO_PIN_6, .rx_af = GPIO_AF7_USART2,
      .baud = 9600,   .databits = 8, .parity = 'n', .stopbits = UART_STOPBITS_1,
      .presc_idx = 0, .kclk = KCLK_PCLK, .fifo = true, .monitor = true },
    { .name = "usart6", .huart = &huart6, .de_port = NULL,
      .rx_gpio = GPIOC, .rx_pin = GPIO_PIN_7, .rx_af = GPIO_AF7_USART6,
      .baud = 19200,  .databits = 8, .parity = 'n', .stopbits = UART_STOPBITS_1,
      .presc_idx = 0, .kclk = KCLK_PCLK, .fifo = true, .monitor = true },
};
#define PORT_COUNT  (sizeof(g_ports) / sizeof(g_ports[0]))

/* Device profiles = canned 'cfg' argument strings. Adding support for a new
 * device is one table entry (plus 'rs485' if it sits behind the RS485 phy). */
typedef struct { const char *name, *desc, *cfg; bool rs485; } Profile;
static const Profile PROFILES[] = {
    { "waveshare8ch", "Waveshare 8CH analog-in, Modbus RTU over RS485",
      "baud=9600 data=8 parity=n stop=1 presc=1 clk=pclk fifo=1 swap=0", true  },
    { "nmea-gps",     "generic NMEA-0183 GPS module",
      "baud=9600 data=8 parity=n stop=1 presc=1 clk=pclk fifo=1 swap=0", false },
    { "ttl-115k2",    "generic 115200 8N1 TTL device (modem/console)",
      "baud=115200 data=8 parity=n stop=1 presc=1 clk=pclk fifo=1 swap=0", false },
    { "uno-accel",    "Arduino UNO fake accelerometer ($ACC ascii / AA55 binary)",
      "baud=9600 data=8 parity=n stop=1 presc=1 clk=pclk fifo=1 swap=0", false },
};
#define PROFILE_COUNT  (sizeof(PROFILES) / sizeof(PROFILES[0]))

/* Desired kernel-clock source per mux group (see file-header caveat).       */
static KClkSel g_kclk_234578 = KCLK_PCLK;   /* USART2/3, UART4/5/7/8         */
static KClkSel g_kclk_16910  = KCLK_PCLK;   /* USART1/6/9/10                 */

static UART_HandleTypeDef *g_console = NULL;

/* ==========================================================================
 *  Console output helpers
 * ========================================================================== */
static void dbg(const char *s)
{
    if (g_console)
        HAL_UART_Transmit(g_console, (uint8_t *)s, (uint16_t)strlen(s), 300);
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

/* ==========================================================================
 *  Lookups
 * ========================================================================== */
static LabPort *port_by_name(const char *n)
{
    for (unsigned i = 0; i < PORT_COUNT; i++)
        if (!strcmp(g_ports[i].name, n)) return &g_ports[i];
    return NULL;
}

static LabPort *port_by_handle(const UART_HandleTypeDef *h)
{
    for (unsigned i = 0; i < PORT_COUNT; i++)
        if (g_ports[i].huart == h) return &g_ports[i];
    return NULL;
}

static bool in_group_234578(const UART_HandleTypeDef *h)
{
    return h->Instance == USART2 || h->Instance == USART3 ||
           h->Instance == UART4;
}

/* ==========================================================================
 *  Kernel clock handling
 * ========================================================================== */

/* MSP hook - runs inside HAL_UART_MspInit() AFTER CubeMX forced the mux to
 * PCLK and BEFORE HAL computes the baud register, so the BRR value ends up
 * matching the clock we actually selected.                                  */
void UARTLAB_ApplyKernelClock(UART_HandleTypeDef *huart)
{
    RCC_PeriphCLKInitTypeDef k = {0};

    if (huart->Instance == USART6) {
        if (g_kclk_16910 == KCLK_PCLK) return;      /* CubeMX default is fine */
        k.PeriphClockSelection = RCC_PERIPHCLK_USART6;
        switch (g_kclk_16910) {
            case KCLK_HSI: k.Usart16ClockSelection = RCC_USART16910CLKSOURCE_HSI; break;
            case KCLK_CSI: k.Usart16ClockSelection = RCC_USART16910CLKSOURCE_CSI; break;
            case KCLK_LSE: k.Usart16ClockSelection = RCC_USART16910CLKSOURCE_LSE; break;
            default: return;
        }
    } else if (in_group_234578(huart)) {
        if (g_kclk_234578 == KCLK_PCLK) return;
        k.PeriphClockSelection = RCC_PERIPHCLK_USART2;   /* any group member  */
        switch (g_kclk_234578) {
            case KCLK_HSI: k.Usart234578ClockSelection = RCC_USART234578CLKSOURCE_HSI; break;
            case KCLK_CSI: k.Usart234578ClockSelection = RCC_USART234578CLKSOURCE_CSI; break;
            case KCLK_LSE: k.Usart234578ClockSelection = RCC_USART234578CLKSOURCE_LSE; break;
            default: return;
        }
    } else {
        return;
    }

    if (HAL_RCCEx_PeriphCLKConfig(&k) != HAL_OK)
        Error_Handler();
}

/* HAL_RCCEx_GetPeriphCLKFreq() on this HAL version only implements the SAI
 * clocks (returns 0 for USARTs), so derive the kernel clock the same way
 * UART_SetConfig() does: from the mux selection we track ourselves.         */
static uint32_t port_kernel_hz(const LabPort *p)
{
    KClkSel sel = (p->huart->Instance == USART6) ? g_kclk_16910 : g_kclk_234578;
    switch (sel) {
        case KCLK_HSI:
            return HSI_VALUE >> ((RCC->CR & RCC_CR_HSIDIV) >> RCC_CR_HSIDIV_Pos);
        case KCLK_CSI:
            return CSI_VALUE;
        case KCLK_LSE:
            return LSE_VALUE;
        default:                    /* PCLK: APB2 for USART6, APB1 for rest  */
            return (p->huart->Instance == USART6) ? HAL_RCC_GetPCLK2Freq()
                                                  : HAL_RCC_GetPCLK1Freq();
    }
}

/* Baud actually achieved with the current kernel clock / prescaler (HAL
 * rounds BRR to the nearest integer). 0 = not reachable (BRR under/overflow) */
static uint32_t port_actual_baud(const LabPort *p)
{
    uint32_t f = port_kernel_hz(p) / PRESC_TAB[p->presc_idx].div;
    if (p->baud == 0) return 0;
    uint32_t div = (f + p->baud / 2U) / p->baud;
    if (div < 0x10U || div > 0xFFFFU) return 0;
    return f / div;
}

/* ==========================================================================
 *  Applying a configuration to the hardware
 * ========================================================================== */
static bool port_apply(LabPort *p)
{
    UART_HandleTypeDef *u = p->huart;

    /* Word length covers payload + parity bit on STM32. */
    uint8_t frame = p->databits + (p->parity != 'n' ? 1 : 0);
    uint32_t wl;
    if      (frame == 7) wl = UART_WORDLENGTH_7B;
    else if (frame == 8) wl = UART_WORDLENGTH_8B;
    else if (frame == 9) wl = UART_WORDLENGTH_9B;
    else {
        dbgf("ERR %s: data=%u + parity does not fit a 7/8/9-bit frame\r\n",
             p->name, p->databits);
        return false;
    }

    /* Publish the desired kernel clock for the MSP hook, then re-create the
     * peripheral so MspInit -> hook -> BRR computation see it in order.     */
    if (u->Instance == USART6) g_kclk_16910  = p->kclk;
    else                       g_kclk_234578 = p->kclk;

    HAL_UART_DeInit(u);

    u->Init.BaudRate       = p->baud;
    u->Init.WordLength     = wl;
    u->Init.StopBits       = p->stopbits;
    u->Init.Parity         = (p->parity == 'n') ? UART_PARITY_NONE :
                             (p->parity == 'e') ? UART_PARITY_EVEN
                                                : UART_PARITY_ODD;
    u->Init.Mode           = UART_MODE_TX_RX;
    u->Init.HwFlowCtl      = UART_HWCONTROL_NONE;
    u->Init.OverSampling   = UART_OVERSAMPLING_16;
    u->Init.OneBitSampling = UART_ONE_BIT_SAMPLE_DISABLE;
    u->Init.ClockPrescaler = PRESC_TAB[p->presc_idx].macro;
    u->AdvancedInit.AdvFeatureInit = p->swap ? UART_ADVFEATURE_SWAP_INIT
                                             : UART_ADVFEATURE_NO_INIT;
    u->AdvancedInit.Swap   = UART_ADVFEATURE_SWAP_ENABLE;

    if (HAL_UART_Init(u) != HAL_OK) {
        p->open = false;
        dbgf("ERR %s: HAL_UART_Init failed - baud unreachable with clk=%s presc=%u?\r\n",
             p->name, KCLK_NAMES[p->kclk], PRESC_TAB[p->presc_idx].div);
        return false;
    }

    if (p->fifo) {
        HAL_UARTEx_SetTxFifoThreshold(u, UART_TXFIFO_THRESHOLD_1_8);
        HAL_UARTEx_SetRxFifoThreshold(u, UART_RXFIFO_THRESHOLD_1_8);
        HAL_UARTEx_EnableFifoMode(u);
    } else {
        HAL_UARTEx_DisableFifoMode(u);
    }

    if (p->rs485 && p->de_port)
        HAL_GPIO_WritePin(p->de_port, p->de_pin, GPIO_PIN_RESET); /* listen  */

    /* Pull the RX pin up: with nothing connected the line then idles at the
     * UART's rest level instead of floating low and streaming break/0x00.   */
    if (p->rx_gpio) {
        GPIO_InitTypeDef g = {0};
        g.Pin       = p->rx_pin;
        g.Mode      = GPIO_MODE_AF_PP;
        g.Pull      = GPIO_PULLUP;
        g.Speed     = GPIO_SPEED_FREQ_LOW;
        g.Alternate = p->rx_af;
        HAL_GPIO_Init(p->rx_gpio, &g);
    }

    p->rlen = 0;
    p->open = true;

    /* Changing the 234578 group clock also moved the console's kernel clock:
     * recompute its BRR. gState is READY so MspInit (and the CubeMX PCLK
     * reset inside it) is skipped - only the divider is recalculated.       */
    if (in_group_234578(u) && g_console) {
        if (HAL_UART_Init(g_console) != HAL_OK) Error_Handler();
        /* UART_SetConfig() clears FIFOEN on every init: turn the console's
         * RX FIFO back on (see UARTLAB_Init).                               */
        HAL_UARTEx_EnableFifoMode(g_console);
    }

    return true;
}

/* ==========================================================================
 *  TX / RX plumbing
 * ========================================================================== */
static void port_tx(LabPort *p, const uint8_t *data, uint16_t len)
{
    if (!p->open) { dbgf("%s is closed\r\n", p->name); return; }

    bool de = p->rs485 && p->de_port;
    if (de) {
        HAL_GPIO_WritePin(p->de_port, p->de_pin, GPIO_PIN_SET);
        HAL_Delay(1);                               /* driver settle          */
    }
    HAL_UART_Transmit(p->huart, (uint8_t *)data, len, 1000);
    if (de) {
        while (__HAL_UART_GET_FLAG(p->huart, UART_FLAG_TC) == RESET) { }
        HAL_GPIO_WritePin(p->de_port, p->de_pin, GPIO_PIN_RESET);
    }
    p->tx_bytes += len;
    dbgf("[%s] TX %uB\r\n", p->name, len);
}

/* True while the user has a partially-typed command on the console (with a
 * timeout so an abandoned half-line doesn't mute the monitor forever).      */
static uint8_t  g_cmd_len = 0;
static uint32_t g_last_key = 0;

static bool console_busy(void)
{
    return g_cmd_len > 0 && (HAL_GetTick() - g_last_key) < 10000U;
}

static void print_frame(const char *tag, LabPort *p,
                        const uint8_t *buf, uint16_t len)
{
    dbgf("[%s] %s %uB:", p->name, tag, len);
    for (uint16_t i = 0; i < len; i++) dbgf(" %02X", buf[i]);
    dbg("  |");
    for (uint16_t i = 0; i < len; i++) {
        char c = (char)buf[i];
        dbgf("%c", (c >= 0x20 && c < 0x7F) ? c : '.');
    }
    dbg("|\r\n");
}

static void port_flush_monitor(LabPort *p)
{
    if (p->rlen == 0) return;

    /* All-0x00 bursts = held-low RX line or gross baud mismatch. Printing
     * each one floods the console, so batch them into a rare summary.      */
    bool all_zero = true;
    for (uint16_t i = 0; i < p->rlen; i++)
        if (p->rbuf[i] != 0x00) { all_zero = false; break; }
    if (all_zero) {
        p->zeros_supp += p->rlen;
        p->rlen = 0;
        if (!console_busy() &&
            (HAL_GetTick() - p->last_zero_report) >= 5000U) {
            p->last_zero_report = HAL_GetTick();
            dbgf("[%s] %lu x 0x00 suppressed - wrong baud or RX held low. "
                 "Try 'auto %s'\r\n",
                 p->name, (unsigned long)p->zeros_supp, p->name);
        }
        return;
    }

    /* Hold the burst while a command is being typed, so monitor output can
     * not tear the command line apart. Nothing is recorded here: this runs
     * again on every main-loop pass until Enter, so counting at this point
     * would inflate 'frames' by one per iteration rather than per burst.   */
    if (p->monitor && console_busy()) {
        if (p->rlen >= sizeof(p->rbuf)) {
            p->mon_dropped += p->rlen;
            p->rlen = 0;
        }
        return;
    }

    /* Keep a copy for 'peek' whether or not the monitor prints it.         */
    memcpy(p->lastfrm, p->rbuf, p->rlen);
    p->lastfrm_len = p->rlen;
    p->frames++;

    if (!p->monitor) { p->rlen = 0; return; }

    print_frame("RX", p, p->rbuf, p->rlen);
    p->rlen = 0;
}

static void port_rx_service(LabPort *p)
{
    UART_HandleTypeDef *u = p->huart;
    if (!p->open) return;

    if (__HAL_UART_GET_FLAG(u, UART_FLAG_ORE)) { __HAL_UART_CLEAR_OREFLAG(u); p->err_ore++; }
    if (__HAL_UART_GET_FLAG(u, UART_FLAG_FE))  { __HAL_UART_CLEAR_FEFLAG(u);  p->err_fe++;  }
    if (__HAL_UART_GET_FLAG(u, UART_FLAG_NE))  { __HAL_UART_CLEAR_NEFLAG(u);  p->err_ne++;  }
    if (__HAL_UART_GET_FLAG(u, UART_FLAG_PE))  { __HAL_UART_CLEAR_PEFLAG(u);  p->err_pe++;  }

    while (__HAL_UART_GET_FLAG(u, UART_FLAG_RXNE)) {
        uint8_t b = (uint8_t)(u->Instance->RDR & 0xFF);
        p->rx_bytes++;
        p->last_rx = HAL_GetTick();
        if (p->rlen >= sizeof(p->rbuf)) port_flush_monitor(p);
        p->rbuf[p->rlen++] = b;
    }

    if (p->rlen &&
        (p->rlen >= MON_FLUSH_LEN ||
         (HAL_GetTick() - p->last_rx) >= MON_FLUSH_MS))
        port_flush_monitor(p);
}

/* ==========================================================================
 *  Console commands
 * ========================================================================== */
static void print_help(void)
{
    dbg("\r\n====================== UART LAB CONSOLE ========================\r\n");
    dbg(" ports                     list ports, configs and counters\r\n");
    dbg(" cfg <port> k=v ...        reconfigure + reopen a port. keys:\r\n");
    dbg("     baud=<n> data=7|8|9 parity=n|e|o stop=0.5|1|1.5|2\r\n");
    dbg("     presc=1..256 clk=pclk|hsi|csi|lse fifo=0|1 swap=0|1\r\n");
    dbg(" bind <port> <profile>     apply a named device profile\r\n");
    dbg(" profiles                  list built-in device profiles\r\n");
    dbg(" open <port> / close <port>\r\n");
    dbg(" send <port> <text>        send text + CRLF\r\n");
    dbg(" sendn <port> <text>       send text, no line ending\r\n");
    dbg(" sendhex <port> <hh> ...   send raw bytes, e.g. sendhex usart2 01 03\r\n");
    dbg(" mon <port> 0|1            echo received bytes to this console\r\n");
    dbg(" peek <port>               show latest received burst (use w/ mon 0)\r\n");
    dbg(" auto <port> [ms]          sweep common bauds, apply the one that frames\r\n");
    dbg("                           cleanly (default 400 ms each; use more for a\r\n");
    dbg("                           slow sensor). Works on unknown devices.\r\n");
    dbg(" rs485 <port> 0|1          DE-pin direction control around TX\r\n");
    dbg(" clear <port>              zero the RX/TX/error counters\r\n");
    dbg(" ?                         this help\r\n");
    dbg(" note: clk on uart4/usart2 also retunes usart3+group (shared mux);\r\n");
    dbg("       the console is re-initialised automatically. lse: usart6 only.\r\n");
    dbg("================================================================\r\n");
}

static const char *stop_str(uint32_t sb)
{
    switch (sb) {
        case UART_STOPBITS_0_5: return "0.5";
        case UART_STOPBITS_1_5: return "1.5";
        case UART_STOPBITS_2:   return "2";
        default:                return "1";
    }
}

static void cmd_ports(void)
{
    for (unsigned i = 0; i < PORT_COUNT; i++) {
        LabPort *p = &g_ports[i];
        dbgf("\r\n %-6s %-6s %lu %u%c%s  presc=/%u clk=%s(%luHz) actual=%lu\r\n",
             p->name, p->open ? "OPEN" : "closed",
             (unsigned long)p->baud, p->databits,
             (p->parity == 'n') ? 'N' : (p->parity == 'e') ? 'E' : 'O',
             stop_str(p->stopbits),
             PRESC_TAB[p->presc_idx].div, KCLK_NAMES[p->kclk],
             (unsigned long)port_kernel_hz(p),
             (unsigned long)port_actual_baud(p));
        dbgf("        fifo=%u swap=%u rs485=%u mon=%u  rx=%lu tx=%lu  "
             "ORE=%lu FE=%lu NE=%lu PE=%lu  zeros=%lu dropped=%lu\r\n",
             p->fifo, p->swap, p->rs485, p->monitor,
             (unsigned long)p->rx_bytes, (unsigned long)p->tx_bytes,
             (unsigned long)p->err_ore, (unsigned long)p->err_fe,
             (unsigned long)p->err_ne,  (unsigned long)p->err_pe,
             (unsigned long)p->zeros_supp, (unsigned long)p->mon_dropped);
    }
}

static void cmd_profiles(void)
{
    for (unsigned i = 0; i < PROFILE_COUNT; i++)
        dbgf(" %-12s %s%s\r\n      %s\r\n", PROFILES[i].name, PROFILES[i].desc,
             PROFILES[i].rs485 ? " [rs485]" : "", PROFILES[i].cfg);
}

/* Apply "k=v" tokens onto a port's desired config. Returns false + message
 * on the first invalid token; nothing is written to hardware on failure.    */
static bool apply_kv_tokens(LabPort *p, char *tok[], int ntok)
{
    for (int i = 0; i < ntok; i++) {
        char *eq = strchr(tok[i], '=');
        if (!eq) { dbgf("ERR: '%s' is not k=v\r\n", tok[i]); return false; }
        *eq = '\0';
        const char *k = tok[i], *v = eq + 1;

        if (!strcmp(k, "baud")) {
            long b = atol(v);
            if (b < 300 || b > 12500000) { dbg("ERR: baud out of range\r\n"); return false; }
            p->baud = (uint32_t)b;
        } else if (!strcmp(k, "data")) {
            int d = atoi(v);
            if (d < 7 || d > 9) { dbg("ERR: data=7|8|9\r\n"); return false; }
            p->databits = (uint8_t)d;
        } else if (!strcmp(k, "parity")) {
            if (v[0] != 'n' && v[0] != 'e' && v[0] != 'o') { dbg("ERR: parity=n|e|o\r\n"); return false; }
            p->parity = v[0];
        } else if (!strcmp(k, "stop")) {
            if      (!strcmp(v, "0.5")) p->stopbits = UART_STOPBITS_0_5;
            else if (!strcmp(v, "1"))   p->stopbits = UART_STOPBITS_1;
            else if (!strcmp(v, "1.5")) p->stopbits = UART_STOPBITS_1_5;
            else if (!strcmp(v, "2"))   p->stopbits = UART_STOPBITS_2;
            else { dbg("ERR: stop=0.5|1|1.5|2\r\n"); return false; }
        } else if (!strcmp(k, "presc")) {
            int d = atoi(v), found = -1;
            for (unsigned j = 0; j < PRESC_COUNT; j++)
                if (PRESC_TAB[j].div == d) { found = (int)j; break; }
            if (found < 0) { dbg("ERR: presc=1|2|4|6|8|10|12|16|32|64|128|256\r\n"); return false; }
            p->presc_idx = (uint8_t)found;
        } else if (!strcmp(k, "clk")) {
            int found = -1;
            for (unsigned j = 0; j < 4; j++)
                if (!strcmp(v, KCLK_NAMES[j])) { found = (int)j; break; }
            if (found < 0) { dbg("ERR: clk=pclk|hsi|csi|lse\r\n"); return false; }
            if (found == KCLK_LSE && p->huart->Instance != USART6) {
                dbg("ERR: lse would kill the shared-mux console - usart6 only\r\n");
                return false;
            }
            p->kclk = (KClkSel)found;
        } else if (!strcmp(k, "fifo")) {
            p->fifo = (v[0] == '1');
        } else if (!strcmp(k, "swap")) {
            p->swap = (v[0] == '1');
        } else {
            dbgf("ERR: unknown key '%s'\r\n", k);
            return false;
        }
    }
    return true;
}

static void cmd_bind(LabPort *p, const char *profile_name)
{
    const Profile *prof = NULL;
    for (unsigned i = 0; i < PROFILE_COUNT; i++)
        if (!strcmp(PROFILES[i].name, profile_name)) { prof = &PROFILES[i]; break; }
    if (!prof) { dbg("unknown profile - try 'profiles'\r\n"); return; }

    char buf[96];
    char *tok[12];
    strncpy(buf, prof->cfg, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';

    int n = 0;
    for (char *s = strtok(buf, " "); s && n < 12; s = strtok(NULL, " "))
        tok[n++] = s;

    if (!apply_kv_tokens(p, tok, n)) return;
    p->rs485 = prof->rs485 && (p->de_port != NULL);
    if (port_apply(p))
        dbgf("%s bound to '%s' (%s)\r\n", p->name, prof->name, prof->desc);
}

/* ==========================================================================
 *  Automatic baud detection
 *  Sweep the common rates and, at each, measure how the line actually frames:
 *  framing errors, all-zero bytes, printable characters. The rate that frames
 *  cleanly is the device's rate. This judges the SIGNAL, not the payload, so
 *  it works for binary protocols as well as text, and for devices this bench
 *  has no profile for.
 * ========================================================================== */
static const uint32_t AUTOBAUD_TAB[] = {
    1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800,
};
#define AUTOBAUD_COUNT (sizeof(AUTOBAUD_TAB) / sizeof(AUTOBAUD_TAB[0]))

typedef struct {
    uint32_t baud, bytes, fe, ne, zeros, printable, score;
} AutoResult;

static void autobaud_measure(LabPort *p, uint32_t dwell_ms, AutoResult *r)
{
    UART_HandleTypeDef *u = p->huart;

    memset(r, 0, sizeof(*r));
    r->baud = p->baud;

    /* discard whatever the previous rate left in the receiver */
    __HAL_UART_CLEAR_OREFLAG(u); __HAL_UART_CLEAR_FEFLAG(u);
    __HAL_UART_CLEAR_NEFLAG(u);  __HAL_UART_CLEAR_PEFLAG(u);
    while (__HAL_UART_GET_FLAG(u, UART_FLAG_RXNE)) (void)(u->Instance->RDR);

    uint32_t t0 = HAL_GetTick();
    while ((HAL_GetTick() - t0) < dwell_ms) {
        if (__HAL_UART_GET_FLAG(u, UART_FLAG_ORE)) __HAL_UART_CLEAR_OREFLAG(u);
        if (!__HAL_UART_GET_FLAG(u, UART_FLAG_RXNE)) continue;

        /* FE/NE are raised alongside the byte that carries them, so sample
         * them before reading RDR.                                         */
        if (__HAL_UART_GET_FLAG(u, UART_FLAG_FE)) { __HAL_UART_CLEAR_FEFLAG(u); r->fe++; }
        if (__HAL_UART_GET_FLAG(u, UART_FLAG_NE)) { __HAL_UART_CLEAR_NEFLAG(u); r->ne++; }

        uint8_t b = (uint8_t)(u->Instance->RDR & 0xFF);
        r->bytes++;
        if (b == 0x00) r->zeros++;
        else if ((b >= 0x20 && b < 0x7F) || b == '\r' || b == '\n' || b == '\t')
            r->printable++;
    }
}

/* 0 = unusable. Framing errors dominate because they are the direct symptom
 * of a wrong rate; all-zero bytes catch the receiver-too-fast case; printable
 * characters only break ties, in favour of text protocols.                  */
static uint32_t autobaud_score(const AutoResult *r)
{
    if (r->bytes < 4) return 0;                 /* too little to judge       */
    uint32_t fe_per_k    = (r->fe        * 1000U) / r->bytes;
    uint32_t zero_per_k  = (r->zeros     * 1000U) / r->bytes;
    uint32_t print_per_k = (r->printable * 1000U) / r->bytes;

    uint32_t penalty = fe_per_k + (zero_per_k / 2U);
    if (penalty >= 1000U) return 0;
    return (1000U - penalty) + (print_per_k / 8U);
}

static void cmd_auto(LabPort *p, uint32_t dwell_ms)
{
    uint32_t restore = p->baud;
    AutoResult best = {0};

    if (dwell_ms < 100U)  dwell_ms = 100U;
    if (dwell_ms > 5000U) dwell_ms = 5000U;

    dbgf("[%s] baud sweep, %lu ms per rate - keep the device transmitting\r\n",
         p->name, (unsigned long)dwell_ms);
    dbg("    baud  bytes   FE   NE zeros print  score\r\n");

    for (unsigned i = 0; i < AUTOBAUD_COUNT; i++) {
        AutoResult r;
        p->baud = AUTOBAUD_TAB[i];
        if (!port_apply(p)) continue;

        autobaud_measure(p, dwell_ms, &r);
        r.score = autobaud_score(&r);
        dbgf("  %6lu %6lu %4lu %4lu %5lu %5lu %6lu\r\n",
             (unsigned long)r.baud, (unsigned long)r.bytes, (unsigned long)r.fe,
             (unsigned long)r.ne, (unsigned long)r.zeros,
             (unsigned long)r.printable, (unsigned long)r.score);

        if (r.score > best.score) best = r;
    }

    if (best.score == 0) {
        p->baud = restore;
        port_apply(p);
        dbgf("[%s] nothing framed cleanly: device silent, not wired, or using a\r\n"
             "       rate/framing outside the sweep. Left at %lu baud. Try a longer\r\n"
             "       dwell for a slow sensor, e.g. 'auto %s 2000'.\r\n",
             p->name, (unsigned long)restore, p->name);
        return;
    }

    p->baud = best.baud;
    port_apply(p);
    p->rx_bytes = p->tx_bytes = 0;
    p->err_ore = p->err_fe = p->err_ne = p->err_pe = 0;
    p->zeros_supp = p->mon_dropped = 0;
    dbgf("[%s] detected %lu baud (actual %lu) - applied, counters cleared\r\n",
         p->name, (unsigned long)best.baud, (unsigned long)port_actual_baud(p));
    if (best.printable * 2U < best.bytes)
        dbg("       mostly non-printable: looks like a binary protocol\r\n");
}

static void cmd_sendhex(LabPort *p, char *tok[], int n)
{
    uint8_t buf[64];
    if (n > 64) n = 64;
    for (int i = 0; i < n; i++) {
        char *end;
        long v = strtol(tok[i], &end, 16);
        if (*end != '\0' || v < 0 || v > 0xFF) {
            dbgf("ERR: '%s' is not a hex byte\r\n", tok[i]);
            return;
        }
        buf[i] = (uint8_t)v;
    }
    port_tx(p, buf, (uint16_t)n);
}

static int tokenize(char *line, char *tok[], int max)
{
    int n = 0;
    char *pc = line;
    while (*pc && n < max) {
        while (*pc == ' ' || *pc == '\t') pc++;
        if (!*pc) break;
        tok[n++] = pc;
        while (*pc && *pc != ' ' && *pc != '\t') pc++;
        if (*pc) *pc++ = '\0';
    }
    return n;
}

static void handle_command(char *line)
{
    /* send/sendn transmit the remainder of the line verbatim, spaces and
     * all. tokenize() NUL-terminates tokens *in place*, so keep an untouched
     * copy and locate the payload by the offset of the third token - saving
     * only a pointer into 'line' would leave it truncated at the first
     * space, and tok[] offsets also absorb leading/repeated whitespace.    */
    char raw[96];
    strncpy(raw, line, sizeof(raw) - 1);
    raw[sizeof(raw) - 1] = '\0';

    char *tok[20];
    int n = tokenize(line, tok, 20);

    if (n == 0) return;

    char *raw_payload = NULL;
    if (n >= 3 && (!strcmp(tok[0], "send") || !strcmp(tok[0], "sendn")))
        raw_payload = raw + (tok[2] - line);

    if (!strcmp(tok[0], "?") || !strcmp(tok[0], "help")) { print_help(); dbg("> "); return; }
    if (!strcmp(tok[0], "ports"))    { cmd_ports();    dbg("> "); return; }
    if (!strcmp(tok[0], "profiles")) { cmd_profiles(); dbg("> "); return; }

    /* Everything below is '<cmd> <port> ...' */
    LabPort *p = (n >= 2) ? port_by_name(tok[1]) : NULL;
    if (!p) { dbg("usage: <cmd> <uart4|usart2|usart6> ... - '?' for help\r\n> "); return; }

    if (!strcmp(tok[0], "cfg")) {
        if (n < 3) dbg("cfg needs at least one k=v\r\n");
        else if (apply_kv_tokens(p, &tok[2], n - 2) && port_apply(p)) {
            dbgf("%s reconfigured: %lu baud (actual %lu), clk=%s\r\n",
                 p->name, (unsigned long)p->baud,
                 (unsigned long)port_actual_baud(p), KCLK_NAMES[p->kclk]);
        }
    }
    else if (!strcmp(tok[0], "bind") && n >= 3)  cmd_bind(p, tok[2]);
    else if (!strcmp(tok[0], "open"))            { if (port_apply(p)) dbgf("%s open\r\n", p->name); }
    else if (!strcmp(tok[0], "close"))           { HAL_UART_DeInit(p->huart); p->open = false; dbgf("%s closed\r\n", p->name); }
    else if (!strcmp(tok[0], "send") || !strcmp(tok[0], "sendn")) {
        if (!raw_payload) {
            dbgf("usage: %s <port> <text>\r\n", tok[0]);
        } else if (!strcmp(tok[0], "sendn")) {
            port_tx(p, (uint8_t *)raw_payload, (uint16_t)strlen(raw_payload));
        } else {
            char out[100];
            int len = snprintf(out, sizeof(out), "%s\r\n", raw_payload);
            port_tx(p, (uint8_t *)out, (uint16_t)len);
        }
    }
    else if (!strcmp(tok[0], "sendhex") && n >= 3) cmd_sendhex(p, &tok[2], n - 2);
    else if (!strcmp(tok[0], "mon") && n >= 3)     { p->monitor = (tok[2][0] == '1'); dbgf("%s monitor %s\r\n", p->name, p->monitor ? "ON" : "OFF"); }
    else if (!strcmp(tok[0], "auto"))
        cmd_auto(p, (n >= 3) ? (uint32_t)atoi(tok[2]) : 400U);
    else if (!strcmp(tok[0], "peek")) {
        if (p->lastfrm_len == 0) dbgf("[%s] nothing received yet\r\n", p->name);
        else {
            print_frame("last RX", p, p->lastfrm, p->lastfrm_len);
            dbgf("        %lu bursts, %lu bytes total\r\n",
                 (unsigned long)p->frames, (unsigned long)p->rx_bytes);
        }
    }
    else if (!strcmp(tok[0], "rs485") && n >= 3) {
        if (!p->de_port) dbgf("%s has no DE pin wired\r\n", p->name);
        else { p->rs485 = (tok[2][0] == '1'); dbgf("%s rs485 %s\r\n", p->name, p->rs485 ? "ON" : "OFF"); }
    }
    else if (!strcmp(tok[0], "clear")) {
        p->rx_bytes = p->tx_bytes = 0;
        p->err_ore = p->err_fe = p->err_ne = p->err_pe = 0;
        p->zeros_supp = p->mon_dropped = 0;
        p->frames = 0;
        p->lastfrm_len = 0;
        p->last_zero_report = 0;
        dbgf("%s counters cleared\r\n", p->name);
    }
    else dbg("unknown command - type ? for help\r\n");

    dbg("> ");
}

/* Non-blocking console reader -> line buffer (polled RXNE, echo, backspace) */
static void service_console(void)
{
    static char line[96];

    if (!g_console) return;

    /* Bytes that arrive while a long blocking print runs can overrun the
     * console. An overrun flag left set stops RXNE for good (the console
     * goes deaf until reset), so clear it - and FE/NE - on every pass.    */
    if (__HAL_UART_GET_FLAG(g_console, UART_FLAG_ORE)) __HAL_UART_CLEAR_OREFLAG(g_console);
    if (__HAL_UART_GET_FLAG(g_console, UART_FLAG_FE))  __HAL_UART_CLEAR_FEFLAG(g_console);
    if (__HAL_UART_GET_FLAG(g_console, UART_FLAG_NE))  __HAL_UART_CLEAR_NEFLAG(g_console);

    while (__HAL_UART_GET_FLAG(g_console, UART_FLAG_RXNE) != RESET) {
        uint8_t c = (uint8_t)(g_console->Instance->RDR & 0xFF);
        g_last_key = HAL_GetTick();
        if (c == '\r' || c == '\n') {
            if (g_cmd_len > 0) {
                line[g_cmd_len] = '\0';
                g_cmd_len = 0;               /* unmute the monitor first    */
                dbg("\r\n");
                handle_command(line);
            }
        } else if (c == 0x08 || c == 0x7F) {          /* backspace           */
            if (g_cmd_len > 0) { g_cmd_len--; dbg("\b \b"); }
        } else if (g_cmd_len < sizeof(line) - 1) {
            line[g_cmd_len++] = (char)c;
            HAL_UART_Transmit(g_console, &c, 1, 10);  /* echo                */
        }
    }
}

/* ==========================================================================
 *  Lifecycle
 * ========================================================================== */
void UARTLAB_Init(UART_HandleTypeDef *console_uart)
{
    g_console = console_uart;

    /* 16-byte RX FIFO on the console, so typed characters survive a burst
     * print (CubeMX disables it). HAL_UART_Init() clears it again, so
     * port_apply() re-enables it after re-initialising the console.       */
    HAL_UARTEx_EnableFifoMode(g_console);

    /* Re-apply every port once so the runtime config (FIFO on, RS485 idle
     * direction, ...) matches what 'ports' reports.                         */
    for (unsigned i = 0; i < PORT_COUNT; i++)
        port_apply(&g_ports[i]);

    dbg("\r\n\r\n############################################################\r\n");
    dbg("  Nautilus UART LAB - runtime-configurable UART test bench\r\n");
    dbg("  Console: USART3 @115200   Ports: uart4, usart2(RS485), usart6\r\n");
    dbg("############################################################\r\n");

    /* Bind the device declared on each port, so a known rig is ready to use
     * straight out of reset. 'bind <port> <other>' still overrides at any
     * time, and 'ports' below reports whatever the binding produced.        */
    for (unsigned i = 0; i < PORT_COUNT; i++)
        if (g_ports[i].boot_profile)
            cmd_bind(&g_ports[i], g_ports[i].boot_profile);

    cmd_ports();
    print_help();
    dbg("> ");
}

void UARTLAB_Task(void)
{
    static uint32_t hb = 0;

    service_console();
    for (unsigned i = 0; i < PORT_COUNT; i++)
        port_rx_service(&g_ports[i]);

    /* 2 Hz heartbeat so a hung main loop is visible at a glance */
    if (HAL_GetTick() - hb >= 250) {
        hb = HAL_GetTick();
        HAL_GPIO_TogglePin(STATUS_LED_GPIO_Port, STATUS_LED_Pin);
    }
}
