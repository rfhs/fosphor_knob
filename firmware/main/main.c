// Griffin PowerMate emulator for the Waveshare ESP32-S3-Knob-Touch-LCD-1.8.
//
//   ring rotation  -> PowerMate rotation (one count per detent by default)
//   touch screen   -> PowerMate push button
//   glow ring      -> PowerMate base LED (attract mode by default, see led.c)

#include "esp_log.h"
#include "esp_pm.h"
#include "nvs_flash.h"
#include "display.h"
#include "encoder.h"
#include "haptic.h"
#include "i2c_bus.h"
#include "led.h"
#include "pm_state.h"
#include "touch.h"
#include "usb_powermate.h"
#include "version.h"

static const char *TAG = "main";

void app_main(void)
{
    ESP_LOGI(TAG, "PowerMate emulator %d.%d.%d", PM_FW_VERSION_MAJOR, PM_FW_VERSION_MINOR,
             PM_FW_VERSION_PATCH);

    // Idle at 80 MHz; drivers and the UI raise the clock while they work.
    // 80 MHz keeps the APB clock (backlight PWM, SPI, I2C) unchanged.
    const esp_pm_config_t pm = {
        .max_freq_mhz = CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ,
        .min_freq_mhz = 80,
        .light_sleep_enable = false,     // USB needs the clocks running
    };
    if (esp_pm_configure(&pm) != ESP_OK) {
        ESP_LOGW(TAG, "frequency scaling unavailable");
    }

    // Backlight off first so nothing shows until the panel has its image.
    ESP_ERROR_CHECK(led_init());
    // USB before the rest: it is also the way back into flash mode.
    ESP_ERROR_CHECK(usb_powermate_init());
    ESP_ERROR_CHECK(encoder_init());

    // NVS keeps the haptic calibration and the settings changed at run time.
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "NVS unavailable, calibration and settings won't be kept");
    }

    if (display_init() == ESP_OK) {
        g_pm_stats.display_ok = true;
    } else {
        ESP_LOGE(TAG, "display init failed");
    }

    if (i2c_bus_init() == ESP_OK) {
        if (haptic_init() != ESP_OK) {
            ESP_LOGW(TAG, "no haptic driver, clicks disabled");
        }
        if (touch_init() != ESP_OK) {
            ESP_LOGE(TAG, "touch init failed, no push button");
        }
    } else {
        ESP_LOGE(TAG, "I2C init failed");
    }

    ESP_ERROR_CHECK(led_start());
}
