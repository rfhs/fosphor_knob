#include <stdatomic.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "esp_timer.h"
#include "pm_state.h"

// Cap on rotation waiting to be reported, so a host that stops polling
// doesn't get a long replay of stale turns when it comes back.
#define PM_MAX_PENDING 64
#define MAX_LISTENERS  2    // pm_input_on_change() callbacks

volatile pm_stats_t g_pm_stats;
volatile bool g_pm_boot_hold;

// ---------------------------------------------------------------------------
// LED
// ---------------------------------------------------------------------------

static portMUX_TYPE s_led_lock = portMUX_INITIALIZER_UNLOCKED;
// Same defaults the Linux driver programs when it binds to the device.
static pm_led_state_t s_led = {
    .brightness = 0x80,
    .pulse_speed = 255,
    .pulse_table = 0,
    .pulse_asleep = true,
    .pulse_awake = false,
};
static atomic_uint s_led_cmds;

void pm_led_command(uint16_t wValue, uint16_t wIndex)
{
    uint8_t cmd = wValue & 0xff;

    taskENTER_CRITICAL(&s_led_lock);
    switch (cmd) {
    case PM_LED_CMD_STATIC_BRIGHTNESS:
        s_led.brightness = wIndex & 0xff;
        break;
    case PM_LED_CMD_PULSE_ASLEEP:
        s_led.pulse_asleep = wIndex & 1;
        break;
    case PM_LED_CMD_PULSE_AWAKE:
        s_led.pulse_awake = wIndex & 1;
        break;
    case PM_LED_CMD_PULSE_MODE: {
        // wIndex = (arg << 8) | op, where op 0 = divide, 1 = normal, 2 = multiply.
        // Fold that back into the driver's 0..510 speed scale.
        uint8_t op = wIndex & 0xff;
        uint8_t arg = wIndex >> 8;
        uint8_t table = wValue >> 8;
        s_led.pulse_table = table > 2 ? 2 : table;
        if (op == 0) {
            s_led.pulse_speed = 255 - arg;
        } else if (op == 2) {
            s_led.pulse_speed = 255 + arg;
        } else {
            s_led.pulse_speed = 255;
        }
        break;
    }
    default:
        break;
    }
    taskEXIT_CRITICAL(&s_led_lock);
    atomic_fetch_add(&s_led_cmds, 1);
}

void pm_led_get(pm_led_state_t *out)
{
    taskENTER_CRITICAL(&s_led_lock);
    *out = s_led;
    taskEXIT_CRITICAL(&s_led_lock);
}

uint32_t pm_led_command_count(void)
{
    return atomic_load(&s_led_cmds);
}

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

static atomic_int s_rotation;
static atomic_bool s_touch_button;
static atomic_bool s_injected_button;
static atomic_uint s_last_activity_ms;
static void (*s_on_change[MAX_LISTENERS])(void);

void pm_input_on_change(void (*callback)(void))
{
    for (int i = 0; i < MAX_LISTENERS; i++) {
        if (!s_on_change[i]) {
            s_on_change[i] = callback;
            return;
        }
    }
}

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void activity(void)
{
    atomic_store(&s_last_activity_ms, now_ms());
    for (int i = 0; i < MAX_LISTENERS; i++) {
        if (s_on_change[i]) {
            s_on_change[i]();
        }
    }
}

uint32_t pm_input_idle_ms(void)
{
    return now_ms() - atomic_load(&s_last_activity_ms);
}

void pm_input_add_rotation(int counts)
{
    if (counts) {
        activity();
    }
    int cur = atomic_load(&s_rotation);
    int next;
    do {
        next = cur + counts;
        if (next > PM_MAX_PENDING) {
            next = PM_MAX_PENDING;
        } else if (next < -PM_MAX_PENDING) {
            next = -PM_MAX_PENDING;
        }
    } while (!atomic_compare_exchange_weak(&s_rotation, &cur, next));
}

int pm_input_peek_rotation(int max_abs)
{
    int cur = atomic_load(&s_rotation);
    if (cur > max_abs) {
        return max_abs;
    }
    if (cur < -max_abs) {
        return -max_abs;
    }
    return cur;
}

void pm_input_consume_rotation(int counts)
{
    atomic_fetch_sub(&s_rotation, counts);
}

void pm_input_discard_rotation(void)
{
    atomic_store(&s_rotation, 0);
}

void pm_input_set_touch_button(bool pressed)
{
    if (atomic_exchange(&s_touch_button, pressed) != pressed) {
        activity();
    }
    g_pm_stats.touch_button = pressed;
}

void pm_input_set_injected_button(bool pressed)
{
    if (atomic_exchange(&s_injected_button, pressed) != pressed) {
        activity();
    }
    g_pm_stats.injected_button = pressed;
}

bool pm_input_button(void)
{
    return atomic_load(&s_touch_button) || atomic_load(&s_injected_button);
}

// ---------------------------------------------------------------------------
// Flash confirmation
// ---------------------------------------------------------------------------

#define FLASH_CONFIRM_WINDOW_US (15 * 1000 * 1000)   // the prompt stays up this long after the last request

static portMUX_TYPE s_flash_lock = portMUX_INITIALIZER_UNLOCKED;
static int64_t s_flash_confirm_until;
static int64_t s_flash_confirm_since;

void pm_flash_confirm_request(void)
{
    int64_t now = esp_timer_get_time();
    taskENTER_CRITICAL(&s_flash_lock);
    if (now >= s_flash_confirm_until) {
        s_flash_confirm_since = now;   // a repeated request keeps the original start
    }
    s_flash_confirm_until = now + FLASH_CONFIRM_WINDOW_US;
    taskEXIT_CRITICAL(&s_flash_lock);
}

int64_t pm_flash_confirm_since(void)
{
    taskENTER_CRITICAL(&s_flash_lock);
    int64_t since = s_flash_confirm_since;
    taskEXIT_CRITICAL(&s_flash_lock);
    return since;
}

bool pm_flash_confirm_pending(void)
{
    taskENTER_CRITICAL(&s_flash_lock);
    bool pending = esp_timer_get_time() < s_flash_confirm_until;
    taskEXIT_CRITICAL(&s_flash_lock);
    return pending;
}

void pm_flash_confirm_cancel(void)
{
    taskENTER_CRITICAL(&s_flash_lock);
    s_flash_confirm_until = 0;
    taskEXIT_CRITICAL(&s_flash_lock);
}

// ---------------------------------------------------------------------------
// Encoder pulse log
// ---------------------------------------------------------------------------

static portMUX_TYPE s_log_lock = portMUX_INITIALIZER_UNLOCKED;
static pm_pulse_log_entry_t s_log[PM_PULSE_LOG_LEN];
static unsigned s_log_next;
static unsigned s_log_count;

void pm_pulse_log_add(uint8_t channel, uint8_t low_ms, bool accepted)
{
    pm_pulse_log_entry_t e = {
        .t_ms = (uint16_t)(esp_timer_get_time() / 1000),
        .channel = (uint8_t)((channel & 1) | (accepted ? 0x80 : 0)),
        .low_ms = low_ms,
    };
    taskENTER_CRITICAL(&s_log_lock);
    s_log[s_log_next] = e;
    s_log_next = (s_log_next + 1) % PM_PULSE_LOG_LEN;
    if (s_log_count < PM_PULSE_LOG_LEN) {
        s_log_count++;
    }
    taskEXIT_CRITICAL(&s_log_lock);
}

int pm_pulse_log_read(pm_pulse_log_entry_t *out, int max_entries)
{
    taskENTER_CRITICAL(&s_log_lock);
    int n = s_log_count < (unsigned)max_entries ? (int)s_log_count : max_entries;
    unsigned start = (s_log_next + PM_PULSE_LOG_LEN - n) % PM_PULSE_LOG_LEN;
    for (int i = 0; i < n; i++) {
        out[i] = s_log[(start + i) % PM_PULSE_LOG_LEN];
    }
    taskEXIT_CRITICAL(&s_log_lock);
    return n;
}
