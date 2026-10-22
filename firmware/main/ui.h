// What the screen shows: the PowerMate LED as a glow ring around the rim, and
// in the middle the receiver's frequency and gain when the host application
// sends them, the RF Hackers Sanctuary logo when it doesn't (and for a moment
// at power-up), or a message.
//
// The setters may be called from any task; changes are applied from inside
// LVGL's own task.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "lvgl.h"

typedef struct {
    bool valid;                // false: nothing to show
    bool has_gain;             // false: the receiver has no gain control
    uint64_t freq_hz;
    int16_t gain_tenths;       // gain in 0.1 dB
    int16_t gain_min_tenths;   // range of the gain arc
    int16_t gain_max_tenths;
} ui_values_t;

// What the middle of the screen shows (pm_info_t.screen; 0 = older firmware).
typedef enum {
    UI_SCREEN_READOUT = 1,
    UI_SCREEN_LOGO,
    UI_SCREEN_INVITE,          // attract mode's invitation to try the knob
    UI_SCREEN_NO_HOST,         // "no USB connection" hint
    UI_SCREEN_FLASH_CONFIRM,   // asking for a touch to allow flash mode
} ui_screen_t;

// Also the wire format of the PM_REQ_SNAPSHOT answer (little endian).
typedef struct __attribute__((packed)) {
    uint16_t width;
    uint16_t height;
    uint16_t stride;           // bytes per row
    uint8_t color_format;      // lv_color_format_t, RGB565 = 0x12
    uint8_t reserved;
    uint32_t size;             // bytes
} ui_snapshot_info_t;
_Static_assert(sizeof(ui_snapshot_info_t) == 12, "wire format");

// Build the screen. Call with the LVGL lock held.
esp_err_t ui_init(lv_display_t *disp);

void ui_set_values(const ui_values_t *values);
void ui_get_values(ui_values_t *values);
// PowerMate LED brightness, 0..255
void ui_set_led(uint8_t level);
// Show the "no USB connection" hint in place of the readout.
void ui_set_no_host(bool show);
// Screen rotation in quarter turns clockwise; kept in flash.
void ui_set_rotation(uint8_t quarter_turns);

// Render the current screen into a buffer that ui_snapshot_data() exposes.
// Waits for LVGL's task to do it.
esp_err_t ui_snapshot_take(ui_snapshot_info_t *info);
const uint8_t *ui_snapshot_data(size_t *size);
