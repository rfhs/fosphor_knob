#pragma once

#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

// Requires i2c_bus_init(). Starts the task that reads the touch controller
// and drives the button.
esp_err_t touch_init(void);

// Touch log, for chasing touches that go unnoticed (tools/pmknob.py touches).
// Records what the controller reported whenever registers 0x00-0x03 changed,
// failed reads, and what was made of each contact.
typedef enum {
    TOUCH_OUTCOME_NONE = 0,
    TOUCH_OUTCOME_PRESS,      // became a button press
    TOUCH_OUTCOME_RELEASE,
    TOUCH_OUTCOME_IGNORED,    // started outside the active zone and stayed there
    TOUCH_OUTCOME_PROMPT,     // a touch on the firmware-update prompt: hold it
    TOUCH_OUTCOME_BOOT,       // finger on the screen at power-up
    TOUCH_OUTCOME_CONFIRMED,  // held long enough: rebooting into flash mode
    TOUCH_OUTCOME_NEEDS_LIFT, // finger was down before the prompt came up: lift it first
} touch_outcome_t;

typedef struct __attribute__((packed)) {
    uint16_t seq;        // increments with every entry
    uint16_t t_ms;       // low 16 bits of the uptime
    uint8_t flags;       // bit0: woken by the interrupt (else found by polling),
                         // bit1: read failed; bits 4-7: touch_outcome_t
    uint8_t gesture;     // controller register 0x01
    uint8_t fingers;     // register 0x02, finger count in the low nibble
    uint8_t event;       // top 2 bits of register 0x03: 0 down, 1 up, 2 contact
    uint16_t x, y;
} touch_log_entry_t;
_Static_assert(sizeof(touch_log_entry_t) == 12, "host tool expects 12 bytes");

typedef struct __attribute__((packed)) {
    uint32_t irqs;       // interrupt pulses seen since boot
    uint8_t config[8];   // controller registers 0xEC 0xED 0xEE 0xF9 0xFA 0xFB 0xFC 0xFE at start-up
} touch_log_header_t;
_Static_assert(sizeof(touch_log_header_t) == 12, "host tool expects 12 bytes");

// Header, then the entries after sequence number 'since', oldest first, as
// many as fit in size. Returns the number of bytes written.
size_t touch_log_read(uint16_t since, uint8_t *out, size_t size);
