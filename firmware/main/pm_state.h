// State shared between the input drivers, the LED renderer and the USB side.
#pragma once

#include <stdbool.h>
#include <stdint.h>

// ---------------------------------------------------------------------------
// LED state, as programmed by the host through the PowerMate vendor request
// (bmRequestType 0x41, bRequest 0x01). See drivers/input/misc/powermate.c.
// ---------------------------------------------------------------------------
#define PM_LED_CMD_STATIC_BRIGHTNESS 0x01
#define PM_LED_CMD_PULSE_ASLEEP      0x02
#define PM_LED_CMD_PULSE_AWAKE       0x03
#define PM_LED_CMD_PULSE_MODE        0x04

typedef struct {
    uint8_t brightness;    // static brightness, 0..255
    uint16_t pulse_speed;  // 0..510, 255 = normal, lower = slower, higher = faster
    uint8_t pulse_table;   // 0..2, pulse waveform
    bool pulse_asleep;     // pulse while the host is suspended
    bool pulse_awake;      // pulse while the host is awake
} pm_led_state_t;

void pm_led_command(uint16_t wValue, uint16_t wIndex);
void pm_led_get(pm_led_state_t *out);
uint32_t pm_led_command_count(void);

// A finger held on the screen since power-up is asking for flash mode: the
// screen blinks to say "keep holding".
extern volatile bool g_pm_boot_hold;

// Who decides what the LED ring does right now.
typedef enum {
    PM_LED_MODE_HOST = 0,        // as the host programmed it (attract mode off)
    PM_LED_MODE_IN_USE,          // steady: the knob is being used
    PM_LED_MODE_ATTRACT,         // breathing: idle, inviting someone to use it
    PM_LED_MODE_NO_HOST,         // breathing: no USB connection
} pm_led_mode_t;

// ---------------------------------------------------------------------------
// Input: rotation counts waiting to be reported and the button sources.
// ---------------------------------------------------------------------------
void pm_input_add_rotation(int counts);
// Returns the pending rotation clamped to +-max_abs without consuming it.
int pm_input_peek_rotation(int max_abs);
void pm_input_consume_rotation(int counts);
void pm_input_discard_rotation(void);

void pm_input_set_touch_button(bool pressed);
void pm_input_set_injected_button(bool pressed);
bool pm_input_button(void);
// Time since the knob was last turned, pressed or released (injected test
// events count).
uint32_t pm_input_idle_ms(void);
// Called (from the task that changed it) whenever rotation or a button changes.
// Up to two listeners.
void pm_input_on_change(void (*callback)(void));

// ---------------------------------------------------------------------------
// Flash mode needs the person at the knob to agree: a USB request only asks,
// a finger held on the screen while the prompt is up confirms.
// ---------------------------------------------------------------------------
void pm_flash_confirm_request(void);
bool pm_flash_confirm_pending(void);
// When the pending request came in (esp_timer time).
int64_t pm_flash_confirm_since(void);
void pm_flash_confirm_cancel(void);

// ---------------------------------------------------------------------------
// Diagnostics, readable from the host with the vendor "info" request.
// ---------------------------------------------------------------------------
typedef struct {
    uint32_t enc_steps_cw;
    uint32_t enc_steps_ccw;
    uint32_t enc_short_pulses;   // pulses rejected by the debouncer
    uint32_t reports_sent;
    uint32_t touch_presses;
    uint32_t i2c_errors;
    uint16_t touch_x, touch_y;   // last touch coordinate
    uint8_t touch_chip_id;       // 0 = touch controller not found
    uint8_t touch_fingers;       // last finger count read
    bool haptic_ok;
    uint8_t haptic_status;       // DRV2605L status after calibration
    uint8_t haptic_lra_period;   // measured resonance, units of 98.46 us
    uint8_t haptic_cal_source;   // 0 none, 1 loaded from flash, 2 calibrated this boot
    bool display_ok;
    bool touch_button;
    bool injected_button;
    uint8_t led_level;           // brightness currently rendered, 0..255
    uint8_t display_rotation;    // quarter turns
    bool display_has_values;     // frequency/gain received from the host
    bool plug_hint;              // "no USB connection" hint on screen
    uint16_t ui_fps_x10;         // screen refreshes per second, x10
    uint16_t ui_glow_us;         // time to recompute the LED ring
    uint16_t ui_refresh_ms;      // time of the last screen refresh
    uint8_t led_mode;            // pm_led_mode_t currently rendered
    uint8_t backlight_level;     // backlight, percent of full
    uint8_t screen;              // ui_screen_t: what the middle of the screen shows
} pm_stats_t;

extern volatile pm_stats_t g_pm_stats;

// Encoder pulse log for tuning the debouncer: newest entries overwrite oldest.
#define PM_PULSE_LOG_LEN 32
typedef struct __attribute__((packed)) {
    uint16_t t_ms;      // timestamp (low 16 bits of uptime in ms)
    uint8_t channel;    // bit0: 0 = A (cw), 1 = B (ccw); bit7: set if accepted
    uint8_t low_ms;     // how long the line was held low, saturating at 255
} pm_pulse_log_entry_t;

void pm_pulse_log_add(uint8_t channel, uint8_t low_ms, bool accepted);
// Copies the newest entries into out, oldest first; returns the number copied.
int pm_pulse_log_read(pm_pulse_log_entry_t *out, int max_entries);
