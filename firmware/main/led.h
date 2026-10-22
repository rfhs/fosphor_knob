#pragma once

#include <stdbool.h>
#include "esp_err.h"

// Configure the backlight PWM (initially dark).
esp_err_t led_init(void);
// Turn the backlight on and start rendering the LED. Call after
// nvs_flash_init(): it loads the stored attract settings.
esp_err_t led_start(void);

// Attract mode: breathe after attract_ms without use, steady while in use.
// attract_ms 0 hands the LED to the host, like a real PowerMate. While it
// breathes, the screen takes turns every invite_ms (0 = never) between the
// readout and the invitation to try the knob. Kept in flash; may be called
// from any task.
uint16_t led_get_attract_ms(void);
uint16_t led_get_invite_ms(void);
void led_set_attract(uint16_t attract_ms, uint16_t invite_ms);
// True while attract mode wants the invitation on the screen.
bool led_attract_invite(void);

// The host changed the LED settings: show them now.
void led_kick(void);
