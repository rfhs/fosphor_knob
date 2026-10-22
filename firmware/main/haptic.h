#pragma once

#include <stdint.h>
#include "esp_err.h"

// Effect numbers from the DRV2605L ROM library (datasheet, section 11.2)
#define HAPTIC_STRONG_CLICK  1
#define HAPTIC_DOUBLE_CLICK  10
#define HAPTIC_EFFECT_MAX    123

// What the knob gives feedback for. Each event plays a configurable effect.
typedef enum {
    HAPTIC_EVENT_PRESS = 0,
    HAPTIC_EVENT_RELEASE,
    HAPTIC_EVENT_DETENT,
    HAPTIC_EVENT_COUNT,
} haptic_event_t;

// Requires i2c_bus_init() and NVS. Calibrates the actuator on first boot and
// stores the result. Failure is not fatal: haptic_play() becomes a no-op.
esp_err_t haptic_init(void);
// Play the effect configured for an event; never blocks.
void haptic_event(haptic_event_t event);
// Queue a ROM effect (1..123); never blocks. 0 plays nothing.
void haptic_play(uint8_t effect);
// Change the effect for an event (0 = none) and keep it in flash.
void haptic_set_effect(haptic_event_t event, uint8_t effect);
uint8_t haptic_get_effect(haptic_event_t event);
// Run the auto-calibration again and store the new result.
void haptic_recalibrate(void);
