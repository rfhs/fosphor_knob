// TinyUSB configuration: a single HID interface, nothing else, so the device
// looks exactly like a Griffin PowerMate on the bus.
#pragma once

#include "tusb_option.h"

#define CFG_TUSB_OS                 OPT_OS_FREERTOS
#define CFG_TUSB_DEBUG              0
#define CFG_TUSB_MEM_SECTION
#define CFG_TUSB_MEM_ALIGN          TU_ATTR_ALIGNED(4)

#define CFG_TUD_ENABLED             1
#define CFG_TUD_MAX_SPEED           OPT_MODE_FULL_SPEED
#define CFG_TUD_DWC2_SLAVE_ENABLE   1
#define CFG_TUD_ENDPOINT0_SIZE      8       // same as the original

#define CFG_TUD_HID                 1
// Large enough for the 8-byte feature report the descriptor declares.
#define CFG_TUD_HID_EP_BUFSIZE      8
