// Pin map for the Waveshare ESP32-S3-Knob-Touch-LCD-1.8 (ESP32-S3 side).
// Source: Waveshare demo code for this board.
#pragma once

// ST77916 360x360 round LCD on QSPI
#define BOARD_LCD_HOST      SPI2_HOST
#define BOARD_LCD_CS        14
#define BOARD_LCD_CLK       13
#define BOARD_LCD_D0        15
#define BOARD_LCD_D1        16
#define BOARD_LCD_D2        17
#define BOARD_LCD_D3        18
#define BOARD_LCD_RST       21
#define BOARD_LCD_BL        47
#define BOARD_LCD_H_RES     360
#define BOARD_LCD_V_RES     360

// Shared I2C bus: CST816 touch + DRV2605 haptic driver
#define BOARD_I2C_PORT      I2C_NUM_0
#define BOARD_I2C_SDA       11
#define BOARD_I2C_SCL       12
#define BOARD_TOUCH_ADDR    0x15
#define BOARD_TOUCH_RST     10
#define BOARD_TOUCH_INT     9
#define BOARD_HAPTIC_ADDR   0x5A
// DRV2605L enable. Waveshare's schematic labels GPIO38 as a UART line and shows
// EN tied to 3V3; on the actual board the haptic output only works with this high.
#define BOARD_HAPTIC_EN     38

// Rotary ring. This is a "bidirectional switch" encoder, not quadrature:
// each detent produces one low pulse on A (clockwise) or on B (counter-clockwise).
#define BOARD_ENC_A         8
#define BOARD_ENC_B         7
