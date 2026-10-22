#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

// Vendor requests understood by the device (bmRequestType 0x40 OUT / 0xC0 IN).
// 0x01 is the Griffin PowerMate LED command (the Linux driver sends it as 0x41);
// the 0xF0-0xFD requests are extensions of this firmware: the display readout
// (fed by knob_display.py, for run.py) and maintenance/testing (tools/pmknob.py).
// INJECT, HAPTIC, HAPTIC_SET, SNAPSHOT, SNAPSHOT_READ, FORCE_NO_HOST and I2C
// exist only with CONFIG_PM_TEST_REQUESTS.
#define PM_REQ_LED          0x01
#define PM_REQ_REBOOT       0xF0   // OUT, wValue 0 = restart, 1 = ROM flash mode (asks for a touch
                                   // first, see CONFIG_PM_FLASH_NEEDS_TOUCH), 2 = withdraw that request
#define PM_REQ_INFO         0xF1   // IN, pm_info_t
#define PM_REQ_INJECT       0xF2   // OUT, wValue low byte = int8 rotation, wIndex = button (0xFFFF = unchanged)
#define PM_REQ_PULSE_LOG    0xF3   // IN, array of pm_pulse_log_entry_t
#define PM_REQ_HAPTIC       0xF4   // OUT, wValue = ROM effect to play (1..123), 0xFF = recalibrate
#define PM_REQ_I2C          0xF5   // wValue = (addr << 8) | reg; IN reads wLength bytes,
                                   // OUT writes the low byte of wIndex (also needs CONFIG_PM_DEBUG_I2C)
#define PM_REQ_HAPTIC_SET   0xF6   // OUT, wValue = event (0 press, 1 release, 2 detent),
                                   // wIndex = effect (0 = none); kept in flash
#define PM_REQ_DISPLAY      0xF7   // OUT pm_display_msg_t: frequency/gain readout; IN reads it back
#define PM_REQ_ROTATION     0xF8   // OUT, wValue = screen rotation in quarter turns; kept in flash
#define PM_REQ_SNAPSHOT     0xF9   // IN, render the screen, returns ui_snapshot_info_t
#define PM_REQ_SNAPSHOT_READ 0xFA  // IN, snapshot bytes from offset (wValue << 16) | wIndex
#define PM_REQ_FORCE_NO_HOST 0xFB  // OUT, wValue 1 = show the "no USB connection" hint anyway
#define PM_REQ_ATTRACT      0xFC   // OUT, wValue = attract mode idle time in ms, 0 = host owns the LED;
                                   // wIndex = invitation turn in ms, 0 = never; kept in flash
#define PM_REQ_TOUCH_LOG    0xFD   // IN, touch_log_header_t + touch_log_entry_t after seq wValue

// PM_REQ_DISPLAY payload (little endian), sent by the host application.
typedef struct __attribute__((packed)) {
    uint8_t version;             // 1; other versions are ignored
    uint8_t flags;               // PM_DISPLAY_*
    int16_t gain_tenths;         // gain, 0.1 dB
    int16_t gain_min_tenths;     // gain arc range
    int16_t gain_max_tenths;
    uint64_t freq_hz;
} pm_display_msg_t;
_Static_assert(sizeof(pm_display_msg_t) == 16, "wire format");
#define PM_DISPLAY_VALID    0x01   // clear to blank the readout
#define PM_DISPLAY_NO_GAIN  0x02   // the receiver has no gain control: show no gain

// Start USB as a Griffin PowerMate.
esp_err_t usb_powermate_init(void);
// True once there has been no working USB connection for a few seconds
// (unplugged, plug upside down, or the host asleep).
bool usb_powermate_no_host(void);
// Restart, optionally into the ROM bootloader for flashing over USB.
// Safe to call from any task.
void usb_powermate_request_reboot(bool bootloader);
