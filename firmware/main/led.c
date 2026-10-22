// The PowerMate's base LED, played as the glow ring on the screen.
//
// By default the knob runs the LED itself ("attract mode"): steady while it
// is being used, breathing once it has been left alone for a moment, to
// invite people to try it. With attract mode off it shows whatever the host
// programs, like a real PowerMate. Either way it breathes when there is no
// USB connection. While attract mode breathes, the screen also takes turns
// between the readout and an invitation to try the knob (led_attract_invite()).
//
// The breathing is done by the backlight hardware: the ring stays at full,
// the whole screen breathes through a chain of LEDC fades, and nothing gets
// redrawn, so the CPU can idle. Only pulses the host asks for are drawn on
// the ring, as on a real PowerMate.
//
// While a USB request for flash mode waits for its touch, or a boot hold is
// running, the screen blinks, also on the backlight: the panel's
// tearing-effect line isn't wired, so a redraw can't be timed to the panel's
// refresh, and a ring flipping between off and full shows a tear across it.
// The backlight changes the whole panel at once.

#include <math.h>
#include <stdatomic.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/ledc.h"
#include "esp_attr.h"
#include "esp_check.h"
#include "esp_timer.h"
#include "nvs.h"
#include "sdkconfig.h"
#include "board.h"
#include "led.h"
#include "pm_state.h"
#include "ui.h"
#include "usb_powermate.h"

#define BL_MODE       LEDC_LOW_SPEED_MODE
#define BL_TIMER      LEDC_TIMER_0
#define BL_CHANNEL    LEDC_CHANNEL_0
#define BL_RESOLUTION LEDC_TIMER_10_BIT
#define BL_MAX_DUTY   ((1 << 10) - 1)
#define BL_FREQ_HZ    50000
#define BL_SEGMENTS   12      // linear hardware fades per breath, following the curve
#define FRAME_MS      20      // ring pulses the host asked for
#define CHECK_MS      100     // otherwise: no-USB and prompt changes show within this
#define PANEL_SETTLE_MS 150   // first LVGL frame is on the panel by then
#define MIN_PULSE_PERIOD_MS 40.0f   // one bright and one dark FRAME_MS frame: the fastest pulse that shows
#define BREATHE_TABLE 2       // the pulse shape run.py asks a real PowerMate for
#define BLINK_HALF_MS 250     // 2 blinks a second, under the 3/s photosensitivity limit
#define BLINK_DIM_PERCENT 25  // the prompt stays legible at the dim end

#define NVS_NAMESPACE     "led"
#define NVS_KEY_ATTRACT   "attract_ms"
#define NVS_KEY_INVITE    "invite_ms"

static const char *TAG = "led";
static atomic_uint s_attract_ms = CONFIG_PM_ATTRACT_IDLE_MS;
static atomic_uint s_invite_ms = CONFIG_PM_ATTRACT_INVITE_MS;
static TaskHandle_t s_task;

// What the backlight hardware is doing on its own.
typedef enum {
    BL_STEADY,     // full brightness, the ring shows the LED
    BL_BREATHE,    // idle or no USB
    BL_BLINK,      // asking for a touch
} bl_pattern_t;

typedef struct {
    bl_pattern_t bl;
    float phase;           // position in the breath or the ring's pulse, 0..1
    int64_t segment_end;   // when the running hardware fade finishes
    bool bright;           // blink: the end the backlight is heading for
    int64_t last;          // time of the previous led_step()
} led_state_t;

static void set_level(uint8_t level)
{
    g_pm_stats.led_level = level;
    ui_set_led(level);
}

static uint32_t full_duty(void)
{
    return (uint32_t)lroundf(BL_MAX_DUTY * CONFIG_PM_BACKLIGHT_PERCENT / 100.0f);
}

// Backlight duty for a point of the breath (0..1), never below the floor.
static uint32_t breath_duty(float level)
{
    float floor = CONFIG_PM_BREATHE_MIN_PERCENT / 100.0f;
    return (uint32_t)lroundf(full_duty() * (floor + (1.0f - floor) * level));
}

static void set_backlight_duty(uint32_t duty)
{
    // the thread-safe variant: the fade driver may also be touching the channel
    ledc_set_duty_and_update(BL_MODE, BL_CHANNEL, duty, 0);
}

static uint8_t backlight_percent(void)
{
    return (uint8_t)((ledc_get_duty(BL_MODE, BL_CHANNEL) * 100 + BL_MAX_DUTY / 2) / BL_MAX_DUTY);
}

static void start_fade(uint32_t duty, int ms)
{
    ledc_set_fade_with_time(BL_MODE, BL_CHANNEL, duty, ms);
    ledc_fade_start(BL_MODE, BL_CHANNEL, LEDC_FADE_NO_WAIT);
}

// Speed scale used by the Linux driver: 0..510, 255 = normal. Below 255 the
// device divides its pulse rate, above it multiplies; both effects only get
// dramatic near the ends of the range.
static float pulse_rate(uint16_t speed)
{
    if (speed <= 255) {
        return (speed + 1) / 256.0f;
    }
    if (speed > 510) {
        speed = 510;
    }
    return 256.0f / (511 - speed);
}

static float pulse_shape(uint8_t table, float phase)
{
    float s = 0.5f - 0.5f * cosf(2.0f * (float)M_PI * phase);
    switch (table) {
    case 1:
        return 1.0f - fabsf(2.0f * phase - 1.0f);   // linear ramp up and down
    case 2:
        return s * s;                               // longer dark, sharper peak
    default:
        return s;                                   // smooth breathing
    }
}

static float pulse_period_ms(uint16_t speed)
{
    float period = CONFIG_PM_PULSE_PERIOD_MS / pulse_rate(speed);
    return period < MIN_PULSE_PERIOD_MS ? MIN_PULSE_PERIOD_MS : period;
}

// Attract mode takes turns of invite_ms between the readout and the
// invitation, readout first: whoever just let go still sees what they set.
static bool invite_due(uint32_t attract_ms, uint32_t invite_ms, uint32_t idle_ms)
{
    if (!attract_ms || !invite_ms || idle_ms < attract_ms) {
        return false;
    }
    return (idle_ms - attract_ms) / invite_ms % 2 == 1;
}

// Fallback wake-up a little after a fade should have ended; the fade-end
// interrupt normally comes first.
static TickType_t until(int64_t when, int64_t now)
{
    int64_t left_ms = (when - now) / 1000;
    return left_ms > 0 ? pdMS_TO_TICKS(left_ms + 5) : 1;
}

// One look at the inputs; returns how long until the next one is needed.
static TickType_t led_step(led_state_t *s, int64_t now)
{
    float dt_ms = (now - s->last) / 1000.0f;
    s->last = now;
    bool segment_done = now >= s->segment_end - 2000;   // 2 ms slack for timer rounding

    bool blink = g_pm_boot_hold || pm_flash_confirm_pending();
    pm_led_state_t st;
    pm_led_get(&st);
    uint32_t attract_ms = atomic_load(&s_attract_ms);
    uint32_t idle_ms = pm_input_idle_ms();
    pm_led_mode_t mode = PM_LED_MODE_HOST;
    // A sleeping host and a pulled cable look the same from here (the bus
    // goes quiet), so the PowerMate's "pulse while asleep" setting is not
    // used: without a host it always breathes.
    if (usb_powermate_no_host()) {
        mode = PM_LED_MODE_NO_HOST;
    } else if (attract_ms) {
        mode = idle_ms >= attract_ms ? PM_LED_MODE_ATTRACT : PM_LED_MODE_IN_USE;
    }
    g_pm_stats.led_mode = mode;

    bl_pattern_t want = BL_STEADY;
    if (blink) {
        want = BL_BLINK;
    } else if (mode == PM_LED_MODE_NO_HOST || mode == PM_LED_MODE_ATTRACT) {
        want = BL_BREATHE;
    }
    if (s->bl != want) {
        ledc_fade_stop(BL_MODE, BL_CHANNEL);
        if (want == BL_STEADY) {
            // used again, or the prompt is over: full brightness straight away
            set_backlight_duty(full_duty());
            g_pm_stats.backlight_level = CONFIG_PM_BACKLIGHT_PERCENT;
        }
        s->bl = want;
        s->phase = 0.5f;      // a breath starts at its top, out of the steady glow
        s->bright = true;
        segment_done = true;
    }

    switch (s->bl) {
    case BL_BLINK:
        set_level(255);
        if (segment_done) {
            s->bright = !s->bright;
            start_fade(s->bright ? full_duty() : full_duty() * BLINK_DIM_PERCENT / 100, BLINK_HALF_MS);
            s->segment_end = now + BLINK_HALF_MS * 1000LL;
        }
        g_pm_stats.backlight_level = backlight_percent();
        return until(s->segment_end, now);

    case BL_BREATHE:
        set_level(255);
        if (segment_done) {
            // fade on to the next point of the curve
            s->phase += 1.0f / BL_SEGMENTS;
            s->phase -= floorf(s->phase);
            int ms = CONFIG_PM_BREATHE_PERIOD_MS / BL_SEGMENTS;
            start_fade(breath_duty(pulse_shape(BREATHE_TABLE, s->phase)), ms);
            s->segment_end = now + ms * 1000LL;
        }
        g_pm_stats.backlight_level = backlight_percent();
        return until(s->segment_end, now);

    case BL_STEADY:
        break;
    }

    // Steady backlight: the ring shows the host's setting, or full while in use.
    if (mode == PM_LED_MODE_HOST && st.pulse_awake) {
        // the host asked for pulsing: drawn on the ring
        s->phase += dt_ms / pulse_period_ms(st.pulse_speed);
        s->phase -= floorf(s->phase);
        set_level((uint8_t)lroundf(255.0f * pulse_shape(st.pulse_table, s->phase)));
        return pdMS_TO_TICKS(FRAME_MS);
    }
    // Every waveform peaks at phase 0.5: starting there, a pulse begins by
    // fading out of the steady glow instead of dropping to black.
    s->phase = 0.5f;
    TickType_t wait = pdMS_TO_TICKS(CHECK_MS);
    if (mode == PM_LED_MODE_IN_USE) {
        set_level(255);
        // wake up right when attract mode is due
        TickType_t left = pdMS_TO_TICKS(attract_ms - idle_ms) + 1;
        if (left < wait) {
            wait = left;
        }
    } else {
        set_level(st.brightness);
    }
    return wait;
}

// Wake the LED task: a backlight fade ended...
static bool IRAM_ATTR fade_done(const ledc_cb_param_t *param, void *arg)
{
    BaseType_t woken = pdFALSE;
    if (param->event == LEDC_FADE_END_EVT) {
        vTaskNotifyGiveFromISR(s_task, &woken);
    }
    return woken == pdTRUE;
}

// ...someone touched the knob, which ends the breathing at once, or the host
// changed the LED.
void led_kick(void)
{
    if (s_task) {
        xTaskNotifyGive(s_task);
    }
}

static void led_task(void *arg)
{
    vTaskDelay(pdMS_TO_TICKS(PANEL_SETTLE_MS));
    set_backlight_duty(full_duty());
    g_pm_stats.backlight_level = CONFIG_PM_BACKLIGHT_PERCENT;
    led_state_t s = {
        .bl = BL_STEADY,
        .phase = 0.5f,
        .last = esp_timer_get_time(),
    };
    for (;;) {
        TickType_t wait = led_step(&s, esp_timer_get_time());
        ulTaskNotifyTake(pdTRUE, wait);
    }
}

esp_err_t led_init(void)
{
    const ledc_timer_config_t timer = {
        .speed_mode = BL_MODE,
        .duty_resolution = BL_RESOLUTION,
        .timer_num = BL_TIMER,
        .freq_hz = BL_FREQ_HZ,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ESP_RETURN_ON_ERROR(ledc_timer_config(&timer), TAG, "timer config failed");
    const ledc_channel_config_t channel = {
        .gpio_num = BOARD_LCD_BL,
        .speed_mode = BL_MODE,
        .channel = BL_CHANNEL,
        .timer_sel = BL_TIMER,
        .duty = 0,     // dark until the panel has its first frame
    };
    ESP_RETURN_ON_ERROR(ledc_channel_config(&channel), TAG, "channel config failed");
    return ledc_fade_func_install(0);
}

uint16_t led_get_attract_ms(void)
{
    return (uint16_t)atomic_load(&s_attract_ms);
}

uint16_t led_get_invite_ms(void)
{
    return (uint16_t)atomic_load(&s_invite_ms);
}

void led_set_attract(uint16_t attract_ms, uint16_t invite_ms)
{
    atomic_store(&s_attract_ms, attract_ms);
    atomic_store(&s_invite_ms, invite_ms);
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_set_u16(nvs, NVS_KEY_ATTRACT, attract_ms);
        nvs_set_u16(nvs, NVS_KEY_INVITE, invite_ms);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
    led_kick();
}

bool led_attract_invite(void)
{
    return !usb_powermate_no_host() &&
           invite_due(atomic_load(&s_attract_ms), atomic_load(&s_invite_ms), pm_input_idle_ms());
}

esp_err_t led_start(void)
{
    nvs_handle_t nvs;
    uint16_t ms;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) == ESP_OK) {
        if (nvs_get_u16(nvs, NVS_KEY_ATTRACT, &ms) == ESP_OK) {
            atomic_store(&s_attract_ms, ms);
        }
        if (nvs_get_u16(nvs, NVS_KEY_INVITE, &ms) == ESP_OK) {
            atomic_store(&s_invite_ms, ms);
        }
        nvs_close(nvs);
    }
    BaseType_t ok = xTaskCreatePinnedToCore(led_task, "led", 3072, NULL, 3, &s_task, 1);
    ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_ERR_NO_MEM, TAG, "no task");
    ledc_cbs_t cbs = { .fade_cb = fade_done };
    ESP_RETURN_ON_ERROR(ledc_cb_register(BL_MODE, BL_CHANNEL, &cbs, NULL), TAG, "fade callback failed");
    pm_input_on_change(led_kick);
    return ESP_OK;
}
