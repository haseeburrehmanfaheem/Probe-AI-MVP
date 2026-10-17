/**
  ******************************************************************************
  * @file    uart_lab.h
  * @brief   Generic runtime-configurable UART test bench.
  *
  *  Every lab port (UART4, USART2, USART6) can be reconfigured live from the
  *  debug console (USART3 / ST-Link VCP @ 115200): baud rate, data bits,
  *  parity, stop bits, UART clock prescaler, kernel clock source, FIFO mode,
  *  TX/RX pin swap and RS485 driver-enable handling.
  *
  *  A PC-side script drives the whole thing by sending console commands
  *  (type '?' on the VCP for the command list).
  ******************************************************************************
  */
#ifndef UART_LAB_H
#define UART_LAB_H

#include "main.h"
#include <stdbool.h>
#include <stdint.h>

/* Call once after all MX_*_UART_Init() calls. console_uart = USART3 handle. */
void UARTLAB_Init(UART_HandleTypeDef *console_uart);

/* Call continuously from the main loop: services console + all open ports.  */
void UARTLAB_Task(void);

/* MSP hook: re-applies the lab's chosen kernel-clock source after CubeMX's
 * HAL_UART_MspInit() has forced it back to PCLK. Called from the USER CODE
 * sections of HAL_UART_MspInit() in stm32h7xx_hal_msp.c.                    */
void UARTLAB_ApplyKernelClock(UART_HandleTypeDef *huart);

#endif /* UART_LAB_H */
