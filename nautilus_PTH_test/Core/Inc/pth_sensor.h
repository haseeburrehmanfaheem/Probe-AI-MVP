/**
  ******************************************************************************
  * @file    pth_sensor.h
  * @brief   PTH (Temperature + Humidity) 4-20mA transmitter driver via a
  *          Waveshare "Modbus RTU Analog Input 8CH" module over RS485.
  *
  *  Signal chain:
  *     PTH sensor (2x 4-20mA loops) --> Waveshare 8CH analog input (current in)
  *        --> Modbus RTU / RS485 --> STM32H723 USART2 (huart2, DE = PE4)
  *
  *  Debug / test console is on USART3 (huart3, 115200 8N1) = ST-Link VCP.
  *  Type '?' + Enter in a serial terminal for the interactive test menu.
  ******************************************************************************
  */
#ifndef PTH_SENSOR_H
#define PTH_SENSOR_H

#ifdef __cplusplus
extern "C" {
#endif

#include "main.h"
#include <stdint.h>
#include <stdbool.h>

/* ==========================================================================
 *  USER CONFIG  -- verify these against YOUR sensor's datasheet before trust!
 * ========================================================================== */

/* Waveshare module Modbus slave address (factory default = 0x01). Runtime
 * changeable from the console with:  addr <n>                                */
#define PTH_DEFAULT_SLAVE_ADDR      0x01

/* Which Waveshare input channels the two 4-20mA loops are wired to.
 * Channel N is Modbus INPUT register (0x04) at address N (0-based).
 * Default: humidity on CH1 (reg 0), temperature on CH2 (reg 1).
 * Swap at runtime with:  ch <hum_reg> <temp_reg>                             */
#define PTH_DEFAULT_HUM_CHANNEL     0
#define PTH_DEFAULT_TEMP_CHANNEL    1

/* Engineering ranges that 4mA..20mA maps to. THESE ARE GUESSES for a generic
 * industrial PTH transmitter -- CONFIRM from the sensor label/datasheet.
 * Change at runtime with:  temp <lo> <hi>   /   hum <lo> <hi>                 */
#define PTH_DEFAULT_TEMP_MIN_C     (-40.0f)
#define PTH_DEFAULT_TEMP_MAX_C     ( 80.0f)
#define PTH_DEFAULT_HUM_MIN_PCT    (  0.0f)
#define PTH_DEFAULT_HUM_MAX_PCT    (100.0f)

/* Loop fault thresholds (mA). Below UNDER = broken wire / no sensor power.   */
#define PTH_MA_UNDERRANGE           3.5f
#define PTH_MA_OVERRANGE            21.0f

/* How the raw Waveshare register value is interpreted as current.
 *   PTH_SCALE_UA   : register holds current directly in micro-amps
 *                    (4-20mA -> 4000..20000). This is the usual current mode.
 *   PTH_SCALE_RAW  : register holds 0..4095 ADC counts over a 0-20mA span.
 * If your numbers look wrong on the bench, flip it with:  mode ua | mode raw */
typedef enum {
    PTH_SCALE_UA = 0,
    PTH_SCALE_RAW
} PTH_ScaleMode;
#define PTH_DEFAULT_SCALE_MODE      PTH_SCALE_UA
#define PTH_RAW_FULLSCALE_COUNTS    4095.0f   /* used only in PTH_SCALE_RAW    */
#define PTH_RAW_FULLSCALE_MA        20.0f     /* used only in PTH_SCALE_RAW    */

/* Auto poll period (ms) when continuous mode is ON (console: cont / stop).   */
#define PTH_DEFAULT_POLL_MS         1000

/* ==========================================================================
 *  Types
 * ========================================================================== */
typedef enum {
    PTH_OK = 0,
    PTH_ERR_TIMEOUT,      /* no / short response from module                  */
    PTH_ERR_CRC,          /* CRC mismatch                                     */
    PTH_ERR_ADDR,         /* wrong slave address echoed                       */
    PTH_ERR_FUNC,         /* wrong / exception function code                  */
    PTH_ERR_EXCEPTION,    /* Modbus exception response                        */
    PTH_ERR_UNDERRANGE,   /* current < ~3.5mA : open loop / unpowered sensor  */
    PTH_ERR_OVERRANGE     /* current > 21mA   : short / miswire               */
} PTH_Status;

typedef struct {
    uint16_t   raw_hum;        /* raw Modbus register value, humidity ch      */
    uint16_t   raw_temp;       /* raw Modbus register value, temperature ch   */
    float      hum_mA;         /* decoded loop current, humidity  (mA)        */
    float      temp_mA;        /* decoded loop current, temperature (mA)      */
    float      humidity;       /* engineering value (%RH)                     */
    float      temperature;    /* engineering value (deg C)                   */
    PTH_Status hum_status;
    PTH_Status temp_status;
    bool       hum_valid;
    bool       temp_valid;
} PTH_Reading;

/* ==========================================================================
 *  API
 * ========================================================================== */

/** Bind the driver to its UARTs. Call once after MX_*_UART_Init().
 *  @param modbus_uart  RS485/Modbus link   (expected: &huart2)
 *  @param debug_uart   console / VCP        (expected: &huart3)              */
void PTH_Init(UART_HandleTypeDef *modbus_uart, UART_HandleTypeDef *debug_uart);

/** Call every loop iteration. Services the interactive console and, when
 *  continuous mode is enabled, performs periodic detailed reads.            */
void PTH_Task(void);

/** One-shot read of both channels into @p out. Returns PTH_OK if BOTH ok.   */
PTH_Status PTH_ReadAll(PTH_Reading *out);

/** Pretty-print a full, detailed reading (raw hex, mA, %span, eng, status)
 *  to the debug console.                                                    */
void PTH_PrintReading(const PTH_Reading *r);

/** Human string for a status code.                                          */
const char *PTH_StatusStr(PTH_Status s);

#ifdef __cplusplus
}
#endif
#endif /* PTH_SENSOR_H */
