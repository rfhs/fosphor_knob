// CST816 touch controller, used as the PowerMate's push button.
//
// The knob has no mechanical push switch, so a finger on the screen is the
// button: down while touching, up when the finger lifts. Contacts that start
// near the rim are ignored, because a hand turning the ring tends to brush
// the edge of the glass.
//
// The controller pulses its interrupt line about every 13 ms while a finger
// is down. Between touches the task sleeps until that interrupt (or the
// optional PM_TOUCH_FALLBACK_POLL_MS poll, in case an interrupt ever goes
// missing) wakes it, then reads every POLL_MS until shortly after the finger
// lifts.

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "sdkconfig.h"
#include "board.h"
#include "haptic.h"
#include "i2c_bus.h"
#include "pm_state.h"
#include "touch.h"
#include "usb_powermate.h"

#define REG_DATA          0x00    // 7 bytes: mode, gesture, fingers, xh, xl, yh, yl
#define REG_CHIP_ID       0xA7
#define REG_DIS_AUTOSLEEP 0xFE

#define POLL_MS           10
#define RELEASE_POLLS     2       // consecutive empty polls before a release
#define IDLE_POLLS        10      // empty polls after which we wait for the interrupt
#define ZONE_DECIDE_MS    60      // a contact outside the active zone gets this long to come inside
#define LOG_LEN           64      // touch log entries; must divide 65536 so seq % LOG_LEN survives the wrap
#define BOOT_HOLD_WINDOW_MS 500   // a finger already down this early starts a boot hold
#define FLASH_CONFIRM_HOLD_MS 1500
#define CONFIRM_LIFT_POLLS 10     // flash-mode holds count a lift after 100 ms, so a dropout doesn't restart them
#define CONFIRM_BUZZ_MS   300     // let the confirming double click play out before the reset cuts it

// empty_polls stops counting at IDLE_POLLS
_Static_assert(RELEASE_POLLS <= IDLE_POLLS && CONFIRM_LIFT_POLLS <= IDLE_POLLS, "lift would never be seen");

static const char *TAG = "touch";
static i2c_master_dev_handle_t s_dev;
static TaskHandle_t s_task;
static volatile uint32_t s_irqs;

static portMUX_TYPE s_log_lock = portMUX_INITIALIZER_UNLOCKED;
static touch_log_entry_t s_log[LOG_LEN];
static uint16_t s_log_seq;            // sequence number of the newest entry
static uint16_t s_log_count;          // entries stored, up to LOG_LEN (s_log_seq wraps)
static uint8_t s_config[8];

static void log_sample(bool irq, bool failed, touch_outcome_t outcome, const uint8_t *d, int x, int y)
{
    touch_log_entry_t e = {
        .t_ms = (uint16_t)(esp_timer_get_time() / 1000),
        .flags = (uint8_t)((irq ? 0x01 : 0) | (failed ? 0x02 : 0) | (outcome << 4)),
        .gesture = d[1],
        .fingers = d[2],
        .event = d[3] >> 6,
        .x = (uint16_t)x,
        .y = (uint16_t)y,
    };
    taskENTER_CRITICAL(&s_log_lock);
    e.seq = ++s_log_seq;
    s_log[e.seq % LOG_LEN] = e;
    if (s_log_count < LOG_LEN) {
        s_log_count++;
    }
    taskEXIT_CRITICAL(&s_log_lock);
}

size_t touch_log_read(uint16_t since, uint8_t *out, size_t size)
{
    touch_log_header_t h = { .irqs = s_irqs };
    memcpy(h.config, s_config, sizeof(h.config));
    if (size < sizeof(h)) {
        return 0;
    }
    memcpy(out, &h, sizeof(h));
    size_t len = sizeof(h);
    taskENTER_CRITICAL(&s_log_lock);
    uint16_t newest = s_log_seq;
    uint16_t unseen = (uint16_t)(newest - since);
    if (unseen > s_log_count) {
        unseen = s_log_count;
    }
    for (uint16_t seq = (uint16_t)(newest - unseen + 1); unseen && len + sizeof(touch_log_entry_t) <= size;
         seq++, unseen--) {
        memcpy(out + len, &s_log[seq % LOG_LEN], sizeof(touch_log_entry_t));
        len += sizeof(touch_log_entry_t);
    }
    taskEXIT_CRITICAL(&s_log_lock);
    return len;
}

static void IRAM_ATTR touch_isr(void *arg)
{
    BaseType_t woken = pdFALSE;
    s_irqs++;
    vTaskNotifyGiveFromISR(s_task, &woken);
    portYIELD_FROM_ISR(woken);
}

static bool in_active_zone(int x, int y)
{
    int dx = x - BOARD_LCD_H_RES / 2;
    int dy = y - BOARD_LCD_V_RES / 2;
    int r = CONFIG_PM_TOUCH_ACTIVE_RADIUS;
    return dx * dx + dy * dy <= r * r;
}

static void set_pressed(bool pressed)
{
    pm_input_set_touch_button(pressed);
    haptic_event(pressed ? HAPTIC_EVENT_PRESS : HAPTIC_EVENT_RELEASE);
    if (pressed) {
        g_pm_stats.touch_presses++;
    }
}

// A boot hold or a touch on the flash-mode prompt was held long enough:
// say so, then reboot into the ROM bootloader. Doesn't return.
static void enter_flash_mode(const uint8_t *d, int x, int y)
{
    log_sample(false, false, TOUCH_OUTCOME_CONFIRMED, d, x, y);
    haptic_play(HAPTIC_DOUBLE_CLICK);
    vTaskDelay(pdMS_TO_TICKS(CONFIRM_BUZZ_MS));
    usb_powermate_request_reboot(true);
    vTaskDelay(portMAX_DELAY);
}

static void touch_task(void *arg)
{
    bool pressed = false;
    bool ignoring_contact = false;   // current contact isn't a press: it started on the rim, or is left over from the prompt
    bool boot_hold = false;
    bool confirming = false;         // flash-mode prompt is up
    bool confirm_needs_lift = false; // finger was already down when it appeared
    bool prompt_contact = false;     // the current contact was seen while the prompt was up
    int64_t confirm_hold_since = 0;  // when the confirming touch started, 0 = none
    int64_t contact_since = 0;       // when the current contact began, 0 = none
    int empty_polls = 0;
    bool wait_for_touch = false;
    int64_t start_us = esp_timer_get_time();
    uint8_t prev[4] = { 0 };         // registers 0..3 of the previous read

    for (;;) {
        TickType_t wait = pdMS_TO_TICKS(POLL_MS);
        if (wait_for_touch) {
            wait = CONFIG_PM_TOUCH_FALLBACK_POLL_MS ? pdMS_TO_TICKS(CONFIG_PM_TOUCH_FALLBACK_POLL_MS) : portMAX_DELAY;
        }
        bool irq = ulTaskNotifyTake(pdTRUE, wait) != 0;

        uint8_t d[7] = { 0 };
        if (i2c_bus_read_reg(s_dev, REG_DATA, d, sizeof(d)) != ESP_OK) {
            // Says nothing about the finger, so it isn't an empty poll:
            // log it and read again after POLL_MS.
            memset(d, 0, sizeof(d));
            log_sample(irq, true, TOUCH_OUTCOME_NONE, d, 0, 0);
            wait_for_touch = false;
            continue;
        }
        int fingers = d[2] & 0x0f;
        int x = ((d[3] & 0x0f) << 8) | d[4];
        int y = ((d[5] & 0x0f) << 8) | d[6];
        touch_outcome_t outcome = TOUCH_OUTCOME_NONE;
        // Log what changed, and interrupts between touches that found
        // nothing on the glass.
        bool log = memcmp(d, prev, sizeof(prev)) != 0 || (irq && !fingers && wait_for_touch);
        memcpy(prev, d, sizeof(prev));
        g_pm_stats.touch_fingers = fingers;
        if (fingers) {
            g_pm_stats.touch_x = x;
            g_pm_stats.touch_y = y;
        }
        int64_t now_us = esp_timer_get_time();
        int64_t since_start_ms = (now_us - start_us) / 1000;

        if (fingers) {
            empty_polls = 0;
        } else if (empty_polls < IDLE_POLLS) {
            empty_polls++;
        }
        bool lifted = empty_polls >= RELEASE_POLLS;
        if (fingers && !contact_since) {
            contact_since = now_us;
        } else if (lifted) {
            contact_since = 0;
            prompt_contact = false;
        }
        // Nothing on the glass for a while (and past the boot-hold window):
        // sleep until the controller signals a touch.
        wait_for_touch = !pressed && empty_polls >= IDLE_POLLS && since_start_ms >= BOOT_HOLD_WINDOW_MS;

        // Holding the screen while the knob powers up asks for flash mode: a
        // finger down within BOOT_HOLD_WINDOW_MS of start-up and kept there
        // until CONFIG_PM_BOOT_HOLD_MS after start-up.
        if (!boot_hold && !pressed && fingers && since_start_ms < BOOT_HOLD_WINDOW_MS) {
            boot_hold = true;
            outcome = TOUCH_OUTCOME_BOOT;
            haptic_event(HAPTIC_EVENT_PRESS);
            g_pm_boot_hold = true;
            ESP_LOGI(TAG, "finger down at boot, hold for flash mode");
        }
        if (boot_hold) {
            if (empty_polls >= CONFIRM_LIFT_POLLS) {
                boot_hold = false;
                g_pm_boot_hold = false;
            } else if (since_start_ms >= CONFIG_PM_BOOT_HOLD_MS) {
                ESP_LOGW(TAG, "boot hold complete, rebooting into flash mode");
                enter_flash_mode(d, x, y);
            }
            if (log || outcome) {
                log_sample(irq, false, outcome, d, x, y);
            }
            continue;
        }

        // A USB request for flash mode: the screen asks, a fresh touch held
        // for a moment agrees. The touch is not a PowerMate button press.
        if (pm_flash_confirm_pending()) {
            if (!confirming) {
                confirming = true;
                // Only a finger that was already down when the request came
                // in has to lift first. Judged by time, because this task
                // may only notice the prompt when the approving touch wakes it.
                confirm_needs_lift = contact_since && contact_since < pm_flash_confirm_since();
                if (confirm_needs_lift) {
                    outcome = TOUCH_OUTCOME_NEEDS_LIFT;
                }
                confirm_hold_since = 0;
                if (pressed) {
                    pressed = false;
                    set_pressed(false);
                }
                ESP_LOGW(TAG, "flash mode requested over USB, waiting for a touch");
            }
            if (fingers) {
                prompt_contact = true;
            }
            if (empty_polls >= CONFIRM_LIFT_POLLS) {
                confirm_needs_lift = false;
                confirm_hold_since = 0;
            } else if (fingers && !confirm_needs_lift) {
                if (!confirm_hold_since) {
                    confirm_hold_since = now_us;
                    outcome = TOUCH_OUTCOME_PROMPT;
                    haptic_event(HAPTIC_EVENT_PRESS);   // got it: keep holding
                } else if (now_us - confirm_hold_since >= FLASH_CONFIRM_HOLD_MS * 1000LL) {
                    ESP_LOGW(TAG, "flash mode confirmed on the knob");
                    pm_flash_confirm_cancel();
                    enter_flash_mode(d, x, y);
                }
            }
            if (log || outcome) {
                log_sample(irq, false, outcome, d, x, y);
            }
            continue;
        }
        if (confirming) {
            // Prompt over: a finger still on the screen from it isn't a
            // button press. A touch that only woke this task after the
            // prompt was withdrawn is a fresh one.
            confirming = false;
            ignoring_contact = fingers && prompt_contact;
            prompt_contact = false;
        }

        // A contact presses the button as soon as it is seen inside the
        // active zone. The first reports may still be off (or 0, 0, before
        // the controller has a position), so one that starts outside gets a
        // moment to show up inside before it is written off as a hand on
        // the rim.
        if (!pressed) {
            if (lifted) {
                ignoring_contact = false;
            } else if (fingers && !ignoring_contact) {
                if ((x || y) && in_active_zone(x, y)) {
                    pressed = true;
                    set_pressed(true);
                    outcome = TOUCH_OUTCOME_PRESS;
                } else if (now_us - contact_since >= ZONE_DECIDE_MS * 1000LL) {
                    ignoring_contact = true;
                    outcome = TOUCH_OUTCOME_IGNORED;
                }
            }
        } else if (lifted) {
            pressed = false;
            set_pressed(false);
            outcome = TOUCH_OUTCOME_RELEASE;
        }
        if (log || outcome) {
            log_sample(irq, false, outcome, d, x, y);
        }
    }
}

esp_err_t touch_init(void)
{
    gpio_config_t rst = {
        .pin_bit_mask = 1ULL << BOARD_TOUCH_RST,
        .mode = GPIO_MODE_OUTPUT,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&rst), TAG, "reset gpio failed");
    gpio_set_level(BOARD_TOUCH_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(BOARD_TOUCH_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(60));

    ESP_RETURN_ON_ERROR(i2c_bus_add_device(BOARD_TOUCH_ADDR, 400000, &s_dev), TAG, "add device failed");

    uint8_t id = 0;
    ESP_RETURN_ON_ERROR(i2c_bus_read_reg(s_dev, REG_CHIP_ID, &id, 1), TAG, "not responding");
    g_pm_stats.touch_chip_id = id ? id : 0xff;   // 0 means "not found" in the stats
    // Settings as the controller came up (before DisAutoSleep below), for
    // the touch log header.
    static const uint8_t config_regs[sizeof(s_config)] = { 0xEC, 0xED, 0xEE, 0xF9, 0xFA, 0xFB, 0xFC, 0xFE };
    for (int i = 0; i < sizeof(config_regs); i++) {
        i2c_bus_read_reg(s_dev, config_regs[i], &s_config[i], 1);
    }
    // Keep the controller awake so polling never hits a sleeping chip.
    i2c_bus_write_reg(s_dev, REG_DIS_AUTOSLEEP, 0x01);
    i2c_bus_write_reg(s_dev, REG_DATA, 0x00);   // normal operating mode, as Waveshare's demo init does
    ESP_LOGI(TAG, "CST816 chip id 0x%02x", id);

    BaseType_t ok = xTaskCreatePinnedToCore(touch_task, "touch", 3072, NULL, 4, &s_task, 1);
    ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_ERR_NO_MEM, TAG, "no task");

    const gpio_config_t irq = {
        .pin_bit_mask = 1ULL << BOARD_TOUCH_INT,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .intr_type = GPIO_INTR_NEGEDGE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&irq), TAG, "interrupt gpio failed");
    esp_err_t err = gpio_install_isr_service(0);
    ESP_RETURN_ON_FALSE(err == ESP_OK || err == ESP_ERR_INVALID_STATE, err, TAG, "isr service failed");
    return gpio_isr_handler_add(BOARD_TOUCH_INT, touch_isr, NULL);
}
