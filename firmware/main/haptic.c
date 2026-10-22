// DRV2605L haptic driver and the board's LRA (resonant at about 165 Hz;
// tools/pmknob.py info shows the measured value).
//
// Two board quirks, both found by the teetotum-rs project and not in
// Waveshare's documentation:
//  - The driver's EN pin is GPIO38. With it low the chip still answers on
//    I2C, but its output stage is off (or, floating, barely on).
//  - Never write MODE = 0x80 (device reset): on this board the chip then
//    drops off the bus at 0x5A until the next power cycle.
//
// Playback runs in its own task so callers (the encoder sampler, the touch
// poller) never wait on I2C. Between clicks the chip sits in standby.

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_check.h"
#include "esp_log.h"
#include "nvs.h"
#include "sdkconfig.h"
#include "board.h"
#include "haptic.h"
#include "i2c_bus.h"
#include "pm_state.h"

#define REG_STATUS        0x00
#define REG_MODE          0x01
#define REG_RTPIN         0x02
#define REG_LIBRARY       0x03
#define REG_WAVESEQ1      0x04
#define REG_WAVESEQ2      0x05
#define REG_GO            0x0C
#define REG_OVERDRIVE     0x0D
#define REG_SUSTAINPOS    0x0E
#define REG_SUSTAINNEG    0x0F
#define REG_BREAK         0x10
#define REG_RATED_VOLTAGE 0x16
#define REG_OD_CLAMP      0x17
#define REG_A_CAL_COMP    0x18
#define REG_A_CAL_BEMF    0x19
#define REG_FEEDBACK      0x1A
#define REG_CONTROL1      0x1B
#define REG_CONTROL3      0x1D
#define REG_CONTROL4      0x1E
#define REG_LRA_PERIOD    0x22

#define MODE_INTERNAL_TRIGGER 0x00
#define MODE_AUTO_CALIBRATION 0x07
#define MODE_STANDBY          0x40   // software standby: a few uA instead of ~0.5 mA
#define STANDBY_AFTER_MS      250    // idle this long after a click: go to standby
#define STATUS_DIAG_FAILED    0x08
#define LIBRARY_LRA           6

// LRA closed loop: brake factor 3, medium loop gain, back-EMF gain 2 (chip defaults + LRA bit)
#define FEEDBACK_LRA          0xB6
// Startup boost, drive time 3.1 ms, about half a period: (26 * 0.1 ms) + 0.5 ms
#define CONTROL1_LRA          0x9A
// Closed loop (LRA_OPEN_LOOP clear), other bits at their defaults
#define CONTROL3_LRA          0xA0

#define NVS_NAMESPACE "haptic"
#define NVS_KEY_CAL   "cal"
#define NVS_KEY_FX    "effects"
#define QUEUE_LEN     8

typedef enum {
    CMD_PLAY,
    CMD_SET_EFFECT,
    CMD_RECALIBRATE,
} haptic_cmd_t;

typedef struct {
    uint8_t cmd;
    uint8_t event;    // CMD_SET_EFFECT
    uint8_t effect;   // CMD_PLAY, CMD_SET_EFFECT
} haptic_msg_t;

typedef struct {
    uint8_t comp;       // A_CAL_COMP
    uint8_t bemf;       // A_CAL_BEMF
    uint8_t feedback;   // FEEDBACK, carries the calibrated back-EMF gain
} haptic_cal_t;

static const char *TAG = "haptic";
static i2c_master_dev_handle_t s_dev;
static QueueHandle_t s_queue;
static uint8_t s_effects[HAPTIC_EVENT_COUNT] = {
    [HAPTIC_EVENT_PRESS] = CONFIG_PM_HAPTIC_PRESS_EFFECT,
    [HAPTIC_EVENT_RELEASE] = CONFIG_PM_HAPTIC_RELEASE_EFFECT,
    [HAPTIC_EVENT_DETENT] = CONFIG_PM_HAPTIC_DETENT_EFFECT,
};

static esp_err_t write_reg(uint8_t reg, uint8_t value)
{
    return i2c_bus_write_reg(s_dev, reg, value);
}

static esp_err_t read_reg(uint8_t reg, uint8_t *value)
{
    return i2c_bus_read_reg(s_dev, reg, value, 1);
}

static esp_err_t calibrate(haptic_cal_t *cal)
{
    ESP_RETURN_ON_ERROR(write_reg(REG_MODE, MODE_AUTO_CALIBRATION), TAG, "mode");
    ESP_RETURN_ON_ERROR(write_reg(REG_GO, 1), TAG, "go");
    uint8_t go = 1;
    for (int i = 0; i < 40 && go; i++) {      // 500-700 ms (CONTROL4 = 0x20); give up after 2 s
        vTaskDelay(pdMS_TO_TICKS(50));
        ESP_RETURN_ON_ERROR(read_reg(REG_GO, &go), TAG, "poll");
    }
    uint8_t status;
    ESP_RETURN_ON_ERROR(read_reg(REG_STATUS, &status), TAG, "status");
    ESP_RETURN_ON_ERROR(write_reg(REG_MODE, MODE_INTERNAL_TRIGGER), TAG, "mode");
    g_pm_stats.haptic_status = status;
    if (go || (status & STATUS_DIAG_FAILED)) {
        ESP_LOGW(TAG, "auto-calibration failed (status 0x%02x)", status);
        return ESP_FAIL;
    }
    ESP_RETURN_ON_ERROR(read_reg(REG_A_CAL_COMP, &cal->comp), TAG, "comp");
    ESP_RETURN_ON_ERROR(read_reg(REG_A_CAL_BEMF, &cal->bemf), TAG, "bemf");
    ESP_RETURN_ON_ERROR(read_reg(REG_FEEDBACK, &cal->feedback), TAG, "feedback");
    ESP_LOGI(TAG, "calibrated: comp 0x%02x bemf 0x%02x feedback 0x%02x",
             cal->comp, cal->bemf, cal->feedback);
    return ESP_OK;
}

static bool load_blob(const char *key, void *buf, size_t size)
{
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) != ESP_OK) {
        return false;
    }
    size_t len = size;
    esp_err_t err = nvs_get_blob(nvs, key, buf, &len);
    nvs_close(nvs);
    return err == ESP_OK && len == size;
}

static void save_blob(const char *key, const void *buf, size_t size)
{
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs) != ESP_OK) {
        return;
    }
    nvs_set_blob(nvs, key, buf, size);
    nvs_commit(nvs);
    nvs_close(nvs);
}

static esp_err_t apply_cal(const haptic_cal_t *cal)
{
    ESP_RETURN_ON_ERROR(write_reg(REG_A_CAL_COMP, cal->comp), TAG, "comp");
    ESP_RETURN_ON_ERROR(write_reg(REG_A_CAL_BEMF, cal->bemf), TAG, "bemf");
    return write_reg(REG_FEEDBACK, cal->feedback);
}

static esp_err_t setup_calibration(bool force)
{
    haptic_cal_t cal;
    if (!force && load_blob(NVS_KEY_CAL, &cal, sizeof(cal))) {
        g_pm_stats.haptic_cal_source = 1;
        return apply_cal(&cal);
    }
    if (calibrate(&cal) == ESP_OK) {
        save_blob(NVS_KEY_CAL, &cal, sizeof(cal));
        g_pm_stats.haptic_cal_source = 2;
        return ESP_OK;
    }
    // Uncalibrated closed loop still works, just less precisely.
    g_pm_stats.haptic_cal_source = 0;
    return write_reg(REG_FEEDBACK, FEEDBACK_LRA);
}

static void haptic_task(void *arg)
{
    uint8_t loaded = 0;
    bool awake = false;
    for (;;) {
        haptic_msg_t msg;
        if (xQueueReceive(s_queue, &msg, awake ? pdMS_TO_TICKS(STANDBY_AFTER_MS) : portMAX_DELAY) != pdTRUE) {
            uint8_t go = 0;
            if (read_reg(REG_GO, &go) == ESP_OK && (go & 1)) {
                continue;               // a long effect is still playing
            }
            write_reg(REG_MODE, MODE_STANDBY);
            awake = false;
            continue;
        }
        if (msg.cmd == CMD_RECALIBRATE) {
            setup_calibration(true);    // leaves the chip awake
            awake = true;
            continue;
        }
        if (msg.cmd == CMD_SET_EFFECT) {
            s_effects[msg.event] = msg.effect;
            save_blob(NVS_KEY_FX, s_effects, sizeof(s_effects));
            continue;
        }
        if (!awake) {
            write_reg(REG_MODE, MODE_INTERNAL_TRIGGER);
            awake = true;
        }
        if (msg.effect != loaded) {
            write_reg(REG_WAVESEQ1, msg.effect);
            loaded = msg.effect;
        }
        // Writing GO while an effect is still playing is ignored by the chip,
        // so a fast spin gives a continuous buzz rather than a backlog.
        write_reg(REG_GO, 1);
        uint8_t period;
        if (read_reg(REG_LRA_PERIOD, &period) == ESP_OK) {
            g_pm_stats.haptic_lra_period = period;
        }
    }
}

esp_err_t haptic_init(void)
{
    const gpio_config_t en = {
        .pin_bit_mask = 1ULL << BOARD_HAPTIC_EN,
        .mode = GPIO_MODE_OUTPUT,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&en), TAG, "enable pin");
    gpio_set_level(BOARD_HAPTIC_EN, 1);
    vTaskDelay(pdMS_TO_TICKS(2));

    ESP_RETURN_ON_ERROR(i2c_bus_add_device(BOARD_HAPTIC_ADDR, 400000, &s_dev), TAG, "add device failed");
    uint8_t status;
    ESP_RETURN_ON_ERROR(read_reg(REG_STATUS, &status), TAG, "not responding");
    uint8_t id = status >> 5;
    // 3 = DRV2605, 4 = DRV2604, 6 = DRV2604L, 7 = DRV2605L
    if (id != 3 && id != 4 && id != 6 && id != 7) {
        ESP_LOGW(TAG, "unexpected chip id %u", id);
        return ESP_ERR_NOT_FOUND;
    }

    // Every register we rely on is written explicitly: after a warm restart
    // the chip may still hold whatever the previous firmware left in it.
    static const uint8_t init[][2] = {
        { REG_MODE, MODE_INTERNAL_TRIGGER },   // out of standby
        { REG_RTPIN, 0 },
        { REG_LIBRARY, LIBRARY_LRA },
        { REG_WAVESEQ1, HAPTIC_STRONG_CLICK },
        { REG_WAVESEQ2, 0 },
        { REG_OVERDRIVE, 0 },
        { REG_SUSTAINPOS, 0 },
        { REG_SUSTAINNEG, 0 },
        { REG_BREAK, 0 },
        { REG_RATED_VOLTAGE, 0x3E },           // chip defaults
        { REG_OD_CLAMP, 0x8C },
        { REG_FEEDBACK, FEEDBACK_LRA },
        { REG_CONTROL1, CONTROL1_LRA },
        { REG_CONTROL3, CONTROL3_LRA },
        { REG_CONTROL4, 0x20 },                // 500 ms auto-calibration
    };
    for (size_t i = 0; i < sizeof(init) / sizeof(init[0]); i++) {
        ESP_RETURN_ON_ERROR(write_reg(init[i][0], init[i][1]), TAG, "init write failed");
    }
    ESP_RETURN_ON_ERROR(setup_calibration(false), TAG, "calibration setup failed");
    if (read_reg(REG_STATUS, &status) == ESP_OK) {
        g_pm_stats.haptic_status = status;
    }
    uint8_t saved[HAPTIC_EVENT_COUNT];
    if (load_blob(NVS_KEY_FX, saved, sizeof(saved))) {
        for (int i = 0; i < HAPTIC_EVENT_COUNT; i++) {
            s_effects[i] = saved[i] <= HAPTIC_EFFECT_MAX ? saved[i] : 0;
        }
    }

    write_reg(REG_MODE, MODE_STANDBY);    // the task wakes it for each click
    s_queue = xQueueCreate(QUEUE_LEN, sizeof(haptic_msg_t));
    ESP_RETURN_ON_FALSE(s_queue, ESP_ERR_NO_MEM, TAG, "no queue");
    BaseType_t ok = xTaskCreatePinnedToCore(haptic_task, "haptic", 3072, NULL, 4, NULL, 1);
    ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_ERR_NO_MEM, TAG, "no task");

    g_pm_stats.haptic_ok = true;
    ESP_LOGI(TAG, "DRV260x id %u ready", id);
    return ESP_OK;
}

static void post(haptic_cmd_t cmd, uint8_t event, uint8_t effect)
{
    haptic_msg_t msg = { .cmd = cmd, .event = event, .effect = effect };
    if (s_queue) {
        xQueueSend(s_queue, &msg, 0);
    }
}

void haptic_play(uint8_t effect)
{
    if (effect && effect <= HAPTIC_EFFECT_MAX) {
        post(CMD_PLAY, 0, effect);
    }
}

void haptic_event(haptic_event_t event)
{
    if (event < HAPTIC_EVENT_COUNT) {
        haptic_play(s_effects[event]);
    }
}

void haptic_set_effect(haptic_event_t event, uint8_t effect)
{
    if (event < HAPTIC_EVENT_COUNT && effect <= HAPTIC_EFFECT_MAX) {
        post(CMD_SET_EFFECT, event, effect);
    }
}

uint8_t haptic_get_effect(haptic_event_t event)
{
    return event < HAPTIC_EVENT_COUNT ? s_effects[event] : 0;
}

void haptic_recalibrate(void)
{
    post(CMD_RECALIBRATE, 0, 0);
}
