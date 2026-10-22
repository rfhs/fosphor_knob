#include "esp_check.h"
#include "board.h"
#include "i2c_bus.h"
#include "pm_state.h"

#define I2C_TIMEOUT_MS 20

static const char *TAG = "i2c";

static i2c_master_bus_handle_t s_bus;

esp_err_t i2c_bus_init(void)
{
    const i2c_master_bus_config_t cfg = {
        .i2c_port = BOARD_I2C_PORT,
        .sda_io_num = BOARD_I2C_SDA,
        .scl_io_num = BOARD_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    return i2c_new_master_bus(&cfg, &s_bus);
}

esp_err_t i2c_bus_add_device(uint8_t addr, uint32_t speed_hz, i2c_master_dev_handle_t *out)
{
    const i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = addr,
        .scl_speed_hz = speed_hz,
    };
    return i2c_master_bus_add_device(s_bus, &cfg, out);
}

esp_err_t i2c_bus_read_reg(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t *buf, size_t len)
{
    esp_err_t err = i2c_master_transmit_receive(dev, &reg, 1, buf, len, I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        g_pm_stats.i2c_errors++;
    }
    return err;
}

esp_err_t i2c_bus_write_reg(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t value)
{
    const uint8_t buf[2] = { reg, value };
    esp_err_t err = i2c_master_transmit(dev, buf, sizeof(buf), I2C_TIMEOUT_MS);
    if (err != ESP_OK) {
        g_pm_stats.i2c_errors++;
    }
    return err;
}

esp_err_t i2c_bus_read_addr(uint8_t addr, uint8_t reg, uint8_t *buf, size_t len)
{
    i2c_master_dev_handle_t dev;
    ESP_RETURN_ON_ERROR(i2c_bus_add_device(addr, 100000, &dev), TAG, "add device failed");
    esp_err_t err = i2c_bus_read_reg(dev, reg, buf, len);
    i2c_master_bus_rm_device(dev);
    return err;
}

esp_err_t i2c_bus_write_addr(uint8_t addr, uint8_t reg, uint8_t value)
{
    i2c_master_dev_handle_t dev;
    ESP_RETURN_ON_ERROR(i2c_bus_add_device(addr, 100000, &dev), TAG, "add device failed");
    esp_err_t err = i2c_bus_write_reg(dev, reg, value);
    i2c_master_bus_rm_device(dev);
    return err;
}
