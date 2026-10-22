// USB side: presents the knob to the host as a Griffin PowerMate.
//
// Descriptors are copied byte for byte from a real PowerMate (077d:0410,
// bcdDevice 0.08), so the Linux "powermate" input driver binds to it and
// usbhid ignores it, exactly as with the original hardware.
//
// Input report, 6 bytes, sent whenever the button or the rotation changes:
//   [0] bit 0: button   [1] int8 rotation since last report   [2..5] LED state
// The LED is programmed with vendor request 0x01 (see pm_state.c).

#include <stdatomic.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_check.h"
#include "esp_log.h"
#include "esp_private/usb_phy.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "soc/rtc_cntl_reg.h"
#include "sdkconfig.h"
#include "tusb.h"
#include "device/usbd_pvt.h"
#include "haptic.h"
#include "i2c_bus.h"
#include "led.h"
#include "ui.h"
#include "pm_state.h"
#include "touch.h"
#include "usb_powermate.h"
#include "version.h"

#define POWERMATE_VID       0x077D
#define POWERMATE_PID       0x0410
#define POWERMATE_BCD       0x0008
#define REPORT_LEN          6
#define HOST_STALL_US       (200 * 1000)  // in-flight report this old: host isn't polling
#define NO_HOST_HINT_US     (3 * 1000 * 1000)  // no host this long: show the plug hint
#define HOUSEKEEPING_MS     50     // USB task wake-up when nothing happens

static const char *TAG = "usb";

static const tusb_desc_device_t s_desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = 0,
    .bDeviceSubClass = 0,
    .bDeviceProtocol = 0,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = POWERMATE_VID,
    .idProduct = POWERMATE_PID,
    .bcdDevice = POWERMATE_BCD,
    .iManufacturer = 1,
    .iProduct = 2,
    .iSerialNumber = 0,
    .bNumConfigurations = 1,
};

static const uint8_t s_desc_hid_report[] = {
    0x05, 0x0c, 0x09, 0x01, 0xa1, 0x01, 0x05, 0x09, 0x19, 0x01, 0x29, 0x01, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x01, 0x81, 0x02, 0x75, 0x07, 0x95, 0x01, 0x81, 0x01, 0x05, 0x01, 0x09, 0x33,
    0x15, 0x81, 0x25, 0x7f, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02, 0x05, 0x0c, 0x09, 0x01, 0x15, 0x00,
    0x26, 0xff, 0x00, 0x75, 0x08, 0x95, 0x04, 0x81, 0x02, 0x09, 0x01, 0x75, 0x08, 0x95, 0x01, 0x91,
    0x02, 0x09, 0x01, 0x75, 0x08, 0x95, 0x08, 0xb1, 0x02, 0xc0,
};

static const uint8_t s_desc_config[] = {
    // configuration: 1 interface, self powered (as the original reports), 100 mA
    0x09, 0x02, 0x29, 0x00, 0x01, 0x01, 0x00, 0xc0, 0x32,
    // interface 0: HID, no subclass/protocol, 2 endpoints
    0x09, 0x04, 0x00, 0x00, 0x02, 0x03, 0x00, 0x00, 0x00,
    // HID 1.11, one report descriptor of 74 bytes
    0x09, 0x21, 0x11, 0x01, 0x00, 0x01, 0x22, sizeof(s_desc_hid_report), 0x00,
    // EP1 IN interrupt, 6 bytes, 10 ms
    0x07, 0x05, 0x81, 0x03, 0x06, 0x00, 0x0a,
    // EP1 OUT interrupt, 1 byte, 10 ms
    0x07, 0x05, 0x01, 0x03, 0x01, 0x00, 0x0a,
};

// The original's strings are NUL padded; keep the same descriptor lengths.
static uint16_t s_str_lang[2];
static uint16_t s_str_manufacturer[52 / 2];
static uint16_t s_str_product[46 / 2];

static TaskHandle_t s_usb_task;
static atomic_int s_reboot_request;    // 0 none, 1 restart, 2 ROM flash mode
static bool s_last_button;             // button state in the last report sent
static int64_t s_report_queued_us;     // when that report was queued
static uint8_t s_ctrl_buf[128];        // vendor request data stage
static bool s_force_no_host;           // set by PM_REQ_FORCE_NO_HOST (testing)
static atomic_bool s_no_host;
static atomic_bool s_kick_pending;

static void make_string(uint16_t *desc, size_t desc_bytes, const char *s)
{
    desc[0] = (uint16_t)((TUSB_DESC_STRING << 8) | desc_bytes);
    for (size_t i = 1; i < desc_bytes / 2; i++) {
        desc[i] = *s ? (uint16_t)*s++ : 0;
    }
}

// ---------------------------------------------------------------------------
// TinyUSB descriptor and class callbacks
// ---------------------------------------------------------------------------

uint8_t const *tud_descriptor_device_cb(void)
{
    return (uint8_t const *)&s_desc_device;
}

uint8_t const *tud_descriptor_configuration_cb(uint8_t index)
{
    (void)index;
    return s_desc_config;
}

uint16_t const *tud_descriptor_string_cb(uint8_t index, uint16_t langid)
{
    (void)langid;
    switch (index) {
    case 0:
        return s_str_lang;
    case 1:
        return s_str_manufacturer;
    case 2:
        return s_str_product;
    default:
        return NULL;   // stall, like the original
    }
}

uint8_t const *tud_hid_descriptor_report_cb(uint8_t instance)
{
    (void)instance;
    return s_desc_hid_report;
}

uint16_t tud_hid_get_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t report_type,
                               uint8_t *buffer, uint16_t reqlen)
{
    // The original stalls GET_REPORT for every report type.
    return 0;
}

// The LED is programmed with vendor requests; reports from the host are ignored.
void tud_hid_set_report_cb(uint8_t instance, uint8_t report_id, hid_report_type_t report_type,
                           uint8_t const *buffer, uint16_t bufsize)
{
}

void tud_hid_report_complete_cb(uint8_t instance, uint8_t const *report, uint16_t len)
{
    g_pm_stats.reports_sent++;
}

void tud_mount_cb(void)
{
    ESP_LOGI(TAG, "configured by host");
    pm_input_discard_rotation();
    s_last_button = false;
}

void tud_umount_cb(void)
{
    ESP_LOGI(TAG, "unconfigured");
    // Whatever the host told us to show is stale now.
    const ui_values_t blank = { .valid = false };
    ui_set_values(&blank);
}

void tud_suspend_cb(bool remote_wakeup_en)
{
    ESP_LOGI(TAG, "suspended");
}

void tud_resume_cb(void)
{
    ESP_LOGI(TAG, "resumed");
}

// ---------------------------------------------------------------------------
// Vendor requests
// ---------------------------------------------------------------------------

// PM_REQ_INFO answer. tools/pmknob.py unpacks it by position (INFO_FORMAT), so
// new fields go at the end.
typedef struct __attribute__((packed)) {
    char magic[4];             // "PMEK": how the host tools tell us from a genuine Griffin
    uint8_t info_version;
    uint8_t fw_major;
    uint8_t fw_minor;
    uint8_t fw_patch;
    uint32_t uptime_ms;
    uint32_t led_commands;
    uint16_t led_speed;
    uint8_t led_brightness;
    uint8_t led_table;
    uint8_t led_asleep;
    uint8_t led_awake;
    uint8_t led_level;
    uint8_t usb_suspended;
    int16_t pending_rotation;
    uint8_t button_touch;
    uint8_t button_injected;
    uint8_t button_reported;
    uint8_t touch_chip_id;
    uint8_t touch_fingers;
    uint8_t haptic_ok;
    uint16_t touch_x;
    uint16_t touch_y;
    uint32_t touch_presses;
    uint32_t enc_steps_cw;
    uint32_t enc_steps_ccw;
    uint32_t enc_short_pulses;
    uint32_t reports_sent;
    uint32_t i2c_errors;
    uint8_t display_ok;
    uint8_t haptic_status;
    uint8_t haptic_lra_period;
    uint8_t haptic_cal_source;
    uint8_t haptic_effects[3];   // press, release, detent
    uint8_t display_rotation;
    uint8_t display_has_values;
    uint8_t plug_hint;
    uint16_t ui_fps_x10;
    uint16_t ui_glow_us;
    uint16_t ui_refresh_ms;
    uint16_t attract_ms;
    uint8_t led_mode;            // pm_led_mode_t
    uint8_t flash_confirm;       // waiting for a touch to allow flash mode
    uint8_t backlight_level;     // percent of full
    uint8_t screen;              // ui_screen_t
    uint16_t invite_ms;
} pm_info_t;
_Static_assert(sizeof(pm_info_t) == 84, "host tool expects 84 bytes");

static uint16_t fill_info(uint8_t *buf)
{
    pm_led_state_t led;
    pm_led_get(&led);
    pm_info_t info = {
        .magic = { 'P', 'M', 'E', 'K' },
        .info_version = 1,
        .fw_major = PM_FW_VERSION_MAJOR,
        .fw_minor = PM_FW_VERSION_MINOR,
        .fw_patch = PM_FW_VERSION_PATCH,
        .uptime_ms = (uint32_t)(esp_timer_get_time() / 1000),
        .led_commands = pm_led_command_count(),
        .led_speed = led.pulse_speed,
        .led_brightness = led.brightness,
        .led_table = led.pulse_table,
        .led_asleep = led.pulse_asleep,
        .led_awake = led.pulse_awake,
        .led_level = g_pm_stats.led_level,
        .usb_suspended = tud_suspended(),
        .pending_rotation = (int16_t)pm_input_peek_rotation(INT16_MAX),
        .button_touch = g_pm_stats.touch_button,
        .button_injected = g_pm_stats.injected_button,
        .button_reported = s_last_button,
        .touch_chip_id = g_pm_stats.touch_chip_id,
        .touch_fingers = g_pm_stats.touch_fingers,
        .haptic_ok = g_pm_stats.haptic_ok,
        .touch_x = g_pm_stats.touch_x,
        .touch_y = g_pm_stats.touch_y,
        .touch_presses = g_pm_stats.touch_presses,
        .enc_steps_cw = g_pm_stats.enc_steps_cw,
        .enc_steps_ccw = g_pm_stats.enc_steps_ccw,
        .enc_short_pulses = g_pm_stats.enc_short_pulses,
        .reports_sent = g_pm_stats.reports_sent,
        .i2c_errors = g_pm_stats.i2c_errors,
        .display_ok = g_pm_stats.display_ok,
        .haptic_status = g_pm_stats.haptic_status,
        .haptic_lra_period = g_pm_stats.haptic_lra_period,
        .haptic_cal_source = g_pm_stats.haptic_cal_source,
        .haptic_effects = {
            haptic_get_effect(HAPTIC_EVENT_PRESS),
            haptic_get_effect(HAPTIC_EVENT_RELEASE),
            haptic_get_effect(HAPTIC_EVENT_DETENT),
        },
        .display_rotation = g_pm_stats.display_rotation,
        .display_has_values = g_pm_stats.display_has_values,
        .plug_hint = g_pm_stats.plug_hint,
        .ui_fps_x10 = g_pm_stats.ui_fps_x10,
        .ui_glow_us = g_pm_stats.ui_glow_us,
        .ui_refresh_ms = g_pm_stats.ui_refresh_ms,
        .attract_ms = led_get_attract_ms(),
        .led_mode = g_pm_stats.led_mode,
        .flash_confirm = pm_flash_confirm_pending(),
        .backlight_level = g_pm_stats.backlight_level,
        .screen = g_pm_stats.screen,
        .invite_ms = led_get_invite_ms(),
    };
    memcpy(buf, &info, sizeof(info));
    return sizeof(info);
}

// Answer an IN request, sending no more than the host asked for.
static bool reply(uint8_t rhport, tusb_control_request_t const *req, const void *data, uint32_t len)
{
    if (len > req->wLength) {
        len = req->wLength;
    }
    return tud_control_xfer(rhport, req, (void *)data, (uint16_t)len);
}

bool tud_vendor_control_xfer_cb(uint8_t rhport, uint8_t stage, tusb_control_request_t const *req)
{
    bool dir_in = req->bmRequestType_bit.direction == TUSB_DIR_IN;
    // Requests that carry their argument in wValue/wIndex must not have a
    // data stage; stall anything else instead of answering out of step.
    bool out_no_data = !dir_in && req->wLength == 0;

    switch (req->bRequest) {
    case PM_REQ_LED:
        // The Linux driver sends this as an interface request (0x41); accept
        // any recipient.
        if (!out_no_data) {
            return false;
        }
        if (stage == CONTROL_STAGE_SETUP) {
            pm_led_command(req->wValue, req->wIndex);
            led_kick();
            return tud_control_status(rhport, req);
        }
        return true;

    case PM_REQ_INFO:
        if (!dir_in) {
            return false;
        }
        if (stage == CONTROL_STAGE_SETUP) {
            return reply(rhport, req, s_ctrl_buf, fill_info(s_ctrl_buf));
        }
        return true;

    case PM_REQ_PULSE_LOG:
        if (!dir_in) {
            return false;
        }
        if (stage == CONTROL_STAGE_SETUP) {
            int n = pm_pulse_log_read((pm_pulse_log_entry_t *)s_ctrl_buf,
                                      sizeof(s_ctrl_buf) / sizeof(pm_pulse_log_entry_t));
            return reply(rhport, req, s_ctrl_buf, n * sizeof(pm_pulse_log_entry_t));
        }
        return true;

    case PM_REQ_TOUCH_LOG:
        if (!dir_in) {
            return false;
        }
        if (stage == CONTROL_STAGE_SETUP) {
            size_t len = touch_log_read(req->wValue, s_ctrl_buf, sizeof(s_ctrl_buf));
            return reply(rhport, req, s_ctrl_buf, len);
        }
        return true;

    case PM_REQ_DISPLAY:
        if (stage == CONTROL_STAGE_SETUP) {
            if (dir_in) {
                ui_values_t v;
                ui_get_values(&v);
                pm_display_msg_t msg = {
                    .version = 1,
                    .flags = (v.valid ? PM_DISPLAY_VALID : 0) | (v.has_gain ? 0 : PM_DISPLAY_NO_GAIN),
                    .gain_tenths = v.gain_tenths,
                    .gain_min_tenths = v.gain_min_tenths,
                    .gain_max_tenths = v.gain_max_tenths,
                    .freq_hz = v.freq_hz,
                };
                memcpy(s_ctrl_buf, &msg, sizeof(msg));
                return reply(rhport, req, s_ctrl_buf, sizeof(msg));
            }
            if (req->wLength != sizeof(pm_display_msg_t)) {
                return false;
            }
            return tud_control_xfer(rhport, req, s_ctrl_buf, req->wLength);
        }
        if (stage == CONTROL_STAGE_DATA && !dir_in) {
            pm_display_msg_t msg;
            memcpy(&msg, s_ctrl_buf, sizeof(msg));
            if (msg.version == 1) {
                const ui_values_t v = {
                    .valid = msg.flags & PM_DISPLAY_VALID,
                    .has_gain = !(msg.flags & PM_DISPLAY_NO_GAIN),
                    .freq_hz = msg.freq_hz,
                    .gain_tenths = msg.gain_tenths,
                    .gain_min_tenths = msg.gain_min_tenths,
                    .gain_max_tenths = msg.gain_max_tenths,
                };
                ui_set_values(&v);
            }
        }
        return true;

    case PM_REQ_ROTATION:
        if (!out_no_data || req->wValue > 3) {
            return false;
        }
        if (stage == CONTROL_STAGE_SETUP) {
            ui_set_rotation((uint8_t)req->wValue);
            return tud_control_status(rhport, req);
        }
        return true;

#if CONFIG_PM_TEST_REQUESTS
    case PM_REQ_SNAPSHOT:
        if (!dir_in) {
            return false;
        }
        if (stage == CONTROL_STAGE_SETUP) {
            ui_snapshot_info_t info;
            if (ui_snapshot_take(&info) != ESP_OK) {
                return false;
            }
            memcpy(s_ctrl_buf, &info, sizeof(info));
            return reply(rhport, req, s_ctrl_buf, sizeof(info));
        }
        return true;

    case PM_REQ_SNAPSHOT_READ: {
        if (!dir_in) {
            return false;
        }
        if (stage != CONTROL_STAGE_SETUP) {
            return true;
        }
        size_t size;
        const uint8_t *data = ui_snapshot_data(&size);
        uint32_t offset = ((uint32_t)req->wValue << 16) | req->wIndex;
        if (!data || offset >= size) {
            return false;
        }
        return reply(rhport, req, data + offset, size - offset);
    }

    case PM_REQ_FORCE_NO_HOST:
        if (!out_no_data) {
            return false;
        }
        if (stage == CONTROL_STAGE_SETUP) {
            s_force_no_host = req->wValue & 1;
            return tud_control_status(rhport, req);
        }
        return true;

    case PM_REQ_INJECT:
        if (!out_no_data) {
            return false;
        }
        if (stage == CONTROL_STAGE_SETUP) {
            int8_t rotation = (int8_t)(req->wValue & 0xff);
            if (rotation) {
                pm_input_add_rotation(rotation);
            }
            if (req->wIndex != 0xffff) {
                pm_input_set_injected_button(req->wIndex & 1);
            }
            return tud_control_status(rhport, req);
        }
        return true;

    case PM_REQ_HAPTIC: {
        bool recalibrate = req->wValue == 0xFF;
        bool valid_effect = req->wValue >= 1 && req->wValue <= HAPTIC_EFFECT_MAX;
        if (!out_no_data || !(recalibrate || valid_effect)) {
            return false;
        }
        if (stage == CONTROL_STAGE_SETUP) {
            if (recalibrate) {
                haptic_recalibrate();
            } else {
                haptic_play((uint8_t)req->wValue);
            }
            return tud_control_status(rhport, req);
        }
        return true;
    }

    case PM_REQ_HAPTIC_SET:
        if (!out_no_data || req->wValue >= HAPTIC_EVENT_COUNT || req->wIndex > HAPTIC_EFFECT_MAX) {
            return false;
        }
        if (stage == CONTROL_STAGE_SETUP) {
            haptic_set_effect((haptic_event_t)req->wValue, (uint8_t)req->wIndex);
            return tud_control_status(rhport, req);
        }
        return true;

#if CONFIG_PM_DEBUG_I2C
    case PM_REQ_I2C: {
        if (!dir_in && !out_no_data) {
            return false;
        }
        uint8_t addr = req->wValue >> 8;
        uint8_t reg = req->wValue & 0xff;
        if (stage != CONTROL_STAGE_SETUP) {
            return true;
        }
        if (dir_in) {
            uint16_t len = req->wLength < sizeof(s_ctrl_buf) ? req->wLength : sizeof(s_ctrl_buf);
            if (i2c_bus_read_addr(addr, reg, s_ctrl_buf, len) != ESP_OK) {
                return false;
            }
            return tud_control_xfer(rhport, req, s_ctrl_buf, len);
        }
        if (i2c_bus_write_addr(addr, reg, req->wIndex & 0xff) != ESP_OK) {
            return false;
        }
        return tud_control_status(rhport, req);
    }
#endif
#endif

    case PM_REQ_REBOOT:
        if (!out_no_data) {
            return false;
        }
        if (stage == CONTROL_STAGE_SETUP) {
            return tud_control_status(rhport, req);
        }
        if (stage == CONTROL_STAGE_ACK) {
            // Status stage is done, so the host has its answer before we drop off the bus.
            if (req->wValue == 1) {
#if CONFIG_PM_FLASH_NEEDS_TOUCH
                pm_flash_confirm_request();     // the touch task reboots once confirmed
#else
                usb_powermate_request_reboot(true);
#endif
            } else if (req->wValue == 2) {
                pm_flash_confirm_cancel();
            } else {
                usb_powermate_request_reboot(false);
            }
        }
        return true;

    case PM_REQ_ATTRACT:
        if (!out_no_data) {
            return false;
        }
        if (stage == CONTROL_STAGE_SETUP) {
            led_set_attract(req->wValue, req->wIndex);
            return tud_control_status(rhport, req);
        }
        return true;

    default:
        return false;
    }
}

// ---------------------------------------------------------------------------
// Reports and the USB task
// ---------------------------------------------------------------------------

static void send_reports(void)
{
    // Turns made while nobody listens are dropped, not replayed later.
    if (!tud_mounted() || tud_suspended()) {
        pm_input_discard_rotation();
        s_last_button = false;
        return;
    }
    if (!tud_hid_ready()) {
        // The last report is still waiting for the host. Waiting this long
        // means nobody is polling the endpoint (no driver bound).
        if (esp_timer_get_time() - s_report_queued_us > HOST_STALL_US) {
            pm_input_discard_rotation();
        }
        return;
    }

    bool button = pm_input_button();
    int delta = pm_input_peek_rotation(CONFIG_PM_MAX_DELTA_PER_REPORT);
    if (button == s_last_button && delta == 0) {
        return;
    }

    pm_led_state_t led;
    pm_led_get(&led);
    uint8_t report[REPORT_LEN] = {
        button ? 0x01 : 0x00,
        (uint8_t)(int8_t)delta,
        0x00,
        led.brightness,
        (uint8_t)((led.pulse_asleep ? 0x01 : 0) | (led.pulse_awake ? 0x02 : 0) | (led.pulse_table << 4)),
        (uint8_t)(led.pulse_speed >> 1),
    };
    if (tud_hid_report(0, report, sizeof(report))) {
        pm_input_consume_rotation(delta);
        s_last_button = button;
        s_report_queued_us = esp_timer_get_time();
    }
}

static void reboot_now(bool bootloader)
{
    ESP_LOGW(TAG, "rebooting%s", bootloader ? " into ROM flash mode" : "");
    if (tud_inited()) {
        tud_disconnect();
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    // Hand the internal PHY back to USB-Serial-JTAG: that is what the ROM
    // bootloader speaks, and what esptool expects to find after the restart.
    REG_CLR_BIT(RTC_CNTL_USB_CONF_REG, RTC_CNTL_SW_HW_USB_PHY_SEL);
    if (bootloader) {
        REG_WRITE(RTC_CNTL_OPTION1_REG, RTC_CNTL_FORCE_DOWNLOAD_BOOT);
    }
    esp_restart();
}

// Runs inside tud_task_ext(), which returns once its queue is empty, so
// send_reports() follows straight away.
static void kicked(void *param)
{
    atomic_store(&s_kick_pending, false);
}

// Input changed: wake the USB task now instead of having it poll. At most one
// wake-up is queued, so this never blocks on a full TinyUSB queue.
static void kick(void)
{
    if (s_usb_task && tud_inited() && !atomic_exchange(&s_kick_pending, true)) {
        usbd_defer_func(kicked, NULL, false);
    }
}

// reboot_now() talks to TinyUSB, so it runs in the USB task, between
// tud_task_ext() calls; if that task never started, reboot right here.
void usb_powermate_request_reboot(bool bootloader)
{
    if (s_usb_task == NULL) {
        reboot_now(bootloader);
    }
    atomic_store(&s_reboot_request, bootloader ? 2 : 1);
}

bool usb_powermate_no_host(void)
{
    return atomic_load(&s_no_host);
}

static void usb_task(void *arg)
{
    const tusb_rhport_init_t init = {
        .role = TUSB_ROLE_DEVICE,
        .speed = TUSB_SPEED_FULL,
    };
    if (!tusb_rhport_init(0, &init)) {
        ESP_LOGE(TAG, "TinyUSB init failed");
        s_usb_task = NULL;
        vTaskDelete(NULL);
    }

    int64_t last_host = esp_timer_get_time();
    for (;;) {
        tud_task_ext(HOUSEKEEPING_MS, false);
        // With the USB-C plug the wrong way round the computer talks to the
        // board's other chip, and with it unplugged nobody talks to us at all.
        // There is no VBUS sensing, so a cable pulled while the knob stays
        // powered only shows up as the bus going quiet (suspended): that
        // counts as no host too.
        int64_t now = esp_timer_get_time();
        if (tud_mounted() && !tud_suspended()) {
            last_host = now;
        }
        bool no_host = now - last_host > NO_HOST_HINT_US;
        atomic_store(&s_no_host, no_host);
        ui_set_no_host(s_force_no_host || no_host);
        int reboot = atomic_exchange(&s_reboot_request, 0);
        if (reboot) {
            reboot_now(reboot == 2);
        }
        send_reports();
    }
}

esp_err_t usb_powermate_init(void)
{
    s_str_lang[0] = (TUSB_DESC_STRING << 8) | sizeof(s_str_lang);
    s_str_lang[1] = 0x0409;
    make_string(s_str_manufacturer, sizeof(s_str_manufacturer), "Griffin Technology Inc.");
    make_string(s_str_product, sizeof(s_str_product), "Griffin PowerMate");

    usb_phy_handle_t phy;
    const usb_phy_config_t phy_cfg = {
        .controller = USB_PHY_CTRL_OTG,
        .target = USB_PHY_TARGET_INT,
        .otg_mode = USB_OTG_MODE_DEVICE,
        .otg_speed = USB_PHY_SPEED_FULL,
    };
    ESP_RETURN_ON_ERROR(usb_new_phy(&phy_cfg, &phy), TAG, "USB PHY init failed");

    pm_input_on_change(kick);
    BaseType_t ok = xTaskCreatePinnedToCore(usb_task, "usb", 4096, NULL, 5, &s_usb_task, 0);
    return ok == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}
