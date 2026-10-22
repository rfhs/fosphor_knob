// Shared I2C bus for the touch controller and the haptic driver. Failed
// transfers are counted in g_pm_stats.i2c_errors.
#pragma once

#include <stddef.h>
#include <stdint.h>
#include "driver/i2c_master.h"

esp_err_t i2c_bus_init(void);
esp_err_t i2c_bus_add_device(uint8_t addr, uint32_t speed_hz, i2c_master_dev_handle_t *out);
esp_err_t i2c_bus_read_reg(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t *buf, size_t len);
esp_err_t i2c_bus_write_reg(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t value);
// One-off register access by address, for the USB debug request.
esp_err_t i2c_bus_read_addr(uint8_t addr, uint8_t reg, uint8_t *buf, size_t len);
esp_err_t i2c_bus_write_addr(uint8_t addr, uint8_t reg, uint8_t value);
