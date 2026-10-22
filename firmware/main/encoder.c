// Rotary ring driver.
//
// The ring is a "bidirectional switch": turning clockwise pulls line A low for
// a moment per detent, counter-clockwise does the same on line B. Both lines
// idle high. Sample both lines every millisecond and count a detent when a
// line comes back high after being low for long enough to not be contact
// bounce.
//
// Sampling only runs while the ring moves: a falling edge on either line
// wakes the sampler, which goes back to sleep once both lines have been
// quiet for a while. Pulses last 5 ms or more, so sampling that starts on the
// edge still sees all of every pulse.

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_check.h"
#include "sdkconfig.h"
#include "board.h"
#include "encoder.h"
#include "haptic.h"
#include "pm_state.h"

#define QUIET_MS 200       // both lines idle this long: stop sampling until the next edge

static const char *TAG = "encoder";

typedef struct {
    int gpio;
    int direction;    // +1 clockwise, -1 counter-clockwise
    uint8_t low_ms;   // how long the line has been low so far
} enc_channel_t;

static enc_channel_t s_channels[2] = {
    { .gpio = BOARD_ENC_A, .direction = +1 },
    { .gpio = BOARD_ENC_B, .direction = -1 },
};
static TaskHandle_t s_task;

static void step(int direction)
{
#if CONFIG_PM_REVERSE_ROTATION
    direction = -direction;
#endif
    if (direction > 0) {
        g_pm_stats.enc_steps_cw++;
    } else {
        g_pm_stats.enc_steps_ccw++;
    }
    pm_input_add_rotation(direction * CONFIG_PM_COUNTS_PER_DETENT);
    haptic_event(HAPTIC_EVENT_DETENT);
}

// Returns true while anything is going on (a line is low).
static bool sample(void)
{
    bool busy = false;
    for (int i = 0; i < 2; i++) {
        enc_channel_t *ch = &s_channels[i];
        if (gpio_get_level(ch->gpio) == 0) {
            if (ch->low_ms < UINT8_MAX) {
                ch->low_ms++;
            }
            busy = true;
            continue;
        }
        if (ch->low_ms == 0) {
            continue;
        }
        bool accepted = ch->low_ms >= CONFIG_PM_ENCODER_MIN_PULSE_MS;
        pm_pulse_log_add(i, ch->low_ms, accepted);
        if (accepted) {
            step(ch->direction);
        } else {
            g_pm_stats.enc_short_pulses++;
        }
        ch->low_ms = 0;
    }
    return busy;
}

static void IRAM_ATTR edge_isr(void *arg)
{
    gpio_intr_disable(BOARD_ENC_A);
    gpio_intr_disable(BOARD_ENC_B);
    BaseType_t woken = pdFALSE;
    vTaskNotifyGiveFromISR(s_task, &woken);
    portYIELD_FROM_ISR(woken);
}

static void encoder_task(void *arg)
{
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        TickType_t wake = xTaskGetTickCount();
        int quiet_ms = 0;
        while (quiet_ms < QUIET_MS) {
            vTaskDelayUntil(&wake, 1);    // one tick = 1 ms (CONFIG_FREERTOS_HZ=1000)
            quiet_ms = sample() ? 0 : quiet_ms + 1;
        }
        gpio_intr_enable(BOARD_ENC_A);
        gpio_intr_enable(BOARD_ENC_B);
        // An edge between the last sample and re-enabling would be missed.
        if (gpio_get_level(BOARD_ENC_A) == 0 || gpio_get_level(BOARD_ENC_B) == 0) {
            xTaskNotifyGive(s_task);
        }
    }
}

esp_err_t encoder_init(void)
{
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << BOARD_ENC_A) | (1ULL << BOARD_ENC_B),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_NEGEDGE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&io), TAG, "gpio config failed");

    // Above the UI and input tasks: sampling must not slip behind drawing.
    BaseType_t ok = xTaskCreatePinnedToCore(encoder_task, "encoder", 2560, NULL, 8, &s_task, 0);
    ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_ERR_NO_MEM, TAG, "no task");
    esp_err_t err = gpio_install_isr_service(0);
    ESP_RETURN_ON_FALSE(err == ESP_OK || err == ESP_ERR_INVALID_STATE, err, TAG, "isr service failed");
    ESP_RETURN_ON_ERROR(gpio_isr_handler_add(BOARD_ENC_A, edge_isr, NULL), TAG, "isr A failed");
    ESP_RETURN_ON_ERROR(gpio_isr_handler_add(BOARD_ENC_B, edge_isr, NULL), TAG, "isr B failed");
    return ESP_OK;
}
