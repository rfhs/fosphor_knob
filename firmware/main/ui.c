#include <inttypes.h>
#include <math.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_lvgl_port.h"
#include "esp_pm.h"
#include "esp_timer.h"
#include "nvs.h"
#include "sdkconfig.h"
#include "board.h"
#include "led.h"
#include "pm_state.h"
#include "ui.h"

// Anybody (numbers, titles, captions) is the open font closest to the RF
// Hackers Sanctuary lettering; Noto Sans (labels) for sentences. See fonts/.
LV_FONT_DECLARE(font_freq_64);
LV_FONT_DECLARE(font_value_40);
LV_FONT_DECLARE(font_title_22);
LV_FONT_DECLARE(font_caption_18);
LV_FONT_DECLARE(font_label_22);
LV_IMAGE_DECLARE(rfhs_logo);

// RF Hackers Sanctuary colors
#define COLOR_RFHS_CYAN   0x00ADEE
#define COLOR_RFHS_RED    0xD1002A
#define COLOR_RFHS_DARK   0x302D2F

#define COLOR_TEXT        0xFFFFFF
#define COLOR_TEXT_DIM    0x6E7888
#define COLOR_LABEL       0x8A94A6
#define COLOR_GAIN        0xFF3050   // the RFHS red, lighter so it reads as text on black
#define COLOR_GAIN_ACTIVE 0xFF8095
#define COLOR_TRACK       COLOR_RFHS_DARK
#define COLOR_HINT        0xC8D0DC
#define UPDATE_PERIOD_MS  20
#define SPLASH_MS         5000       // the logo stays this long after power-up, even with values

#define NVS_NAMESPACE     "display"
#define NVS_KEY_ROTATION  "rotation"

static const char *TAG = "ui";

static lv_display_t *s_disp;
static lv_obj_t *s_ring;
static lv_obj_t *s_readout;      // the labels, hidden until values arrive
static lv_obj_t *s_freq;
static lv_obj_t *s_freq_frac;
static lv_obj_t *s_gain;
static lv_obj_t *s_gain_unit;
static lv_obj_t *s_gain_caption;
static lv_obj_t *s_gain_row;
static lv_obj_t *s_logo;         // shown when there is no readout or message
static lv_obj_t *s_message;      // notice that replaces the readout or logo
static lv_obj_t *s_message_icon;
static lv_obj_t *s_message_title;
static lv_obj_t *s_message_body;
static lv_draw_buf_t *s_snapshot;
static atomic_bool s_snapshot_requested;
static SemaphoreHandle_t s_snapshot_done;

// Requests from other tasks, applied by update_timer() in LVGL's task.
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static ui_values_t s_values;
static bool s_values_dirty;
static atomic_int s_led_level = 0x80;
static atomic_bool s_no_host_requested;
static atomic_int s_rotation = -1;    // quarter turns, -1 = none pending

typedef enum {
    MESSAGE_NONE,
    MESSAGE_INVITE,          // attract mode's turn to invite people to try the knob
    MESSAGE_NO_HOST,         // no USB connection: unplugged, plug upside down or host asleep
    MESSAGE_FLASH_CONFIRM,   // a firmware update was requested over USB
} message_t;
static message_t s_message_shown;   // the readout and logo are hidden while a message is up
static uint8_t s_rotation_applied;
static uint32_t s_refreshes;
static int64_t s_refresh_start;
static esp_pm_lock_handle_t s_cpu_lock;  // full speed while drawing, 80 MHz otherwise
static bool s_cpu_fast;

static void cpu_fast(bool fast)
{
    if (s_cpu_lock && fast != s_cpu_fast) {
        s_cpu_fast = fast;
        if (fast) {
            esp_pm_lock_acquire(s_cpu_lock);
        } else {
            esp_pm_lock_release(s_cpu_lock);
        }
    }
}

// ---------------------------------------------------------------------------
// The ring layer: the PowerMate LED glow around the rim plus the gain arc
// just inside it.
//
// The glow changes brightness up to 50 times a second while the host has the
// LED pulsing (idle breathing is done on the backlight, see led.c), so this
// layer is not left to LVGL's general blending (and an lv_arc, whose bounding
// box covers most of the screen, would get redrawn with it). It is one RGB565
// image that we fill ourselves, in two bands that never overlap: the glow
// band is refilled when the LED changes, the arc band when the gain does, and
// only the band that changed is invalidated. The numbers in the middle are
// never re-rendered for a pulse.
// ---------------------------------------------------------------------------

#define GLOW_RADIUS     166.0f   // brightest part of the glow
#define GLOW_INNER      133      // glow band: [GLOW_INNER, edge]; dimmer than 1/40 inside
#define GLOW_STEPS      ((BOARD_LCD_H_RES / 2 + 2 - GLOW_INNER) * 4)  // radius in 1/4 px
#define ARC_RADIUS      124.0f   // centerline of the gain arc
#define ARC_WIDTH       8.0f
#define ARC_WIDTH_ACTIVE 12.0f
#define ARC_START_DEG   135.0f   // 0 = 3 o'clock, clockwise: opens at the bottom
#define ARC_SWEEP_DEG   270.0f
#define ARC_INNER       116      // arc band: [ARC_INNER, ARC_OUTER], room for caps and AA
#define ARC_OUTER       132
_Static_assert(ARC_OUTER < GLOW_INNER, "the arc and glow bands must not overlap");
#define STRIP_HEIGHT    30       // invalidation granularity
#define MAX_RECTS       (2 * (BOARD_LCD_V_RES / STRIP_HEIGHT + 1))

typedef int16_t segs_t[BOARD_LCD_V_RES][4];   // per row: [x0,x1) and [x2,x3)

typedef struct {
    uint32_t idx;      // pixel index
    float x, y;        // position relative to the center
    float r;           // distance from the center
    float deg;         // angle from the arc start, clockwise, in (-45, 315]
    uint8_t track;     // coverage by the (fixed) track, 0..255
} arc_px_t;

typedef struct {
    int16_t gain_tenths, min_tenths, max_tenths;
    bool visible;
    bool active;       // button held: gain is what the ring changes
} arc_state_t;

typedef struct {
    lv_area_t rects[MAX_RECTS];
    int count;
} band_rects_t;

static const uint8_t s_bayer[4][4] = {
    { 0, 8, 2, 10 }, { 12, 4, 14, 6 }, { 3, 11, 1, 9 }, { 15, 7, 13, 5 },
};

static lv_image_dsc_t s_ring_dsc;
static uint16_t *s_ring_px;                // RGB565 image shown by LVGL
static segs_t s_glow_seg;
static uint8_t *s_glow_ridx;               // radius index of each glow band pixel, in row order
static uint8_t s_glow_base[GLOW_STEPS][3]; // RGB888 glow at full brightness by radius index
static band_rects_t s_glow_rects;
static arc_px_t *s_arc_px;
static int s_arc_px_count;
static band_rects_t s_arc_rects;
static arc_state_t s_arc;
static uint32_t s_ring_opa;                // glow brightness, 0..255

static void glow_color(float r, uint8_t rgb[3])
{
    float d = r - GLOW_RADIUS;
    float core = expf(-(d * d) / (5.0f * 5.0f));
    float glow = core + 0.55f * expf(-(d * d) / (12.0f * 12.0f));
    // Light computed linearly, in the RFHS cyan (0.50 green to 1 blue is
    // #00ADEE in linear light); the hot core goes toward white like an LED.
    float lin[3] = {
        0.00f * glow + 0.55f * core * core,
        0.50f * glow + 0.55f * core * core,
        1.00f * glow,
    };
    for (int i = 0; i < 3; i++) {
        rgb[i] = (uint8_t)lroundf(255.0f * powf(fminf(lin[i], 1.0f), 1.0f / 2.2f));
    }
}

// RGB888 to RGB565, rounding with an ordered-dither threshold t (0..255).
static inline uint16_t pack565(uint32_t r8, uint32_t g8, uint32_t b8, uint32_t t)
{
    return (uint16_t)((((r8 * 31 + t) / 255) << 11) | (((g8 * 63 + t) / 255) << 5) | ((b8 * 31 + t) / 255));
}

static inline uint32_t dither_threshold(int x, int y)
{
    return s_bayer[y & 3][x & 3] * 16 + 8;
}

// Row segments of the annulus [inner, outer) clipped to the screen.
static void annulus_segments(segs_t seg, float inner, float outer)
{
    const int w = BOARD_LCD_H_RES;
    const float c = (w - 1) / 2.0f;
    for (int y = 0; y < BOARD_LCD_V_RES; y++) {
        float dy = fabsf(y - c);
        int16_t *s = seg[y];
        s[0] = s[1] = s[2] = s[3] = 0;
        if (dy >= outer) {
            continue;
        }
        int xo = (int)ceilf(sqrtf(outer * outer - dy * dy));
        if (dy < inner) {
            int xi = (int)floorf(sqrtf(inner * inner - dy * dy));
            s[0] = c - xo; s[1] = c - xi + 1;
            s[2] = c + xi; s[3] = c + xo + 1;
        } else {
            s[0] = c - xo; s[1] = c + xo + 1;
        }
        for (int i = 0; i < 4; i++) {
            s[i] = s[i] < 0 ? 0 : (s[i] > w ? w : s[i]);
        }
    }
}

// Cover a band with at most two rectangles per horizontal strip.
static void band_rects(segs_t seg, band_rects_t *out)
{
    const int w = BOARD_LCD_H_RES, h = BOARD_LCD_V_RES;
    out->count = 0;
    for (int y0 = 0; y0 < h; y0 += STRIP_HEIGHT) {
        int y1 = y0 + STRIP_HEIGHT - 1 < h ? y0 + STRIP_HEIGHT - 1 : h - 1;
        int lo = w, hi = -1, gap_l = 0, gap_r = w;
        bool split = true;
        for (int y = y0; y <= y1; y++) {
            const int16_t *s = seg[y];
            if (s[1] <= s[0]) {
                continue;
            }
            bool two = s[3] > s[2];
            lo = s[0] < lo ? s[0] : lo;
            int end = two ? s[3] : s[1];
            hi = end - 1 > hi ? end - 1 : hi;
            if (two) {
                gap_l = s[1] > gap_l ? s[1] : gap_l;
                gap_r = s[2] - 1 < gap_r ? s[2] - 1 : gap_r;
            } else {
                split = false;
            }
        }
        if (hi < lo || out->count + 2 > MAX_RECTS) {
            continue;
        }
        if (split && gap_r > gap_l) {
            out->rects[out->count++] = (lv_area_t){ lo, y0, gap_l - 1, y1 };
            out->rects[out->count++] = (lv_area_t){ gap_r + 1, y0, hi, y1 };
        } else {
            out->rects[out->count++] = (lv_area_t){ lo, y0, hi, y1 };
        }
    }
}

static int count_pixels(segs_t seg)
{
    int n = 0;
    for (int y = 0; y < BOARD_LCD_V_RES; y++) {
        n += seg[y][1] - seg[y][0] + seg[y][3] - seg[y][2];
    }
    return n;
}

static void invalidate_band(const band_rects_t *band)
{
    // LVGL draws the RGB565 buffer directly (no image cache), so invalidating is enough.
    for (int i = 0; i < band->count; i++) {
        lv_obj_invalidate_area(s_ring, &band->rects[i]);
    }
}

typedef struct {
    float end_deg;
    float half_width;
    float slack_deg;                        // angular reach of a cap past its end
    float cap0_x, cap0_y, cap1_x, cap1_y;   // centers of the round caps
} arc_shape_t;

static arc_shape_t arc_shape(float end_deg, float width)
{
    float a0 = ARC_START_DEG * (float)M_PI / 180.0f;
    float a1 = (ARC_START_DEG + end_deg) * (float)M_PI / 180.0f;
    return (arc_shape_t){
        .end_deg = end_deg,
        .half_width = width / 2,
        .slack_deg = (width / 2 + 1.5f) / ARC_RADIUS * 180.0f / (float)M_PI,
        .cap0_x = ARC_RADIUS * cosf(a0), .cap0_y = ARC_RADIUS * sinf(a0),
        .cap1_x = ARC_RADIUS * cosf(a1), .cap1_y = ARC_RADIUS * sinf(a1),
    };
}

// Coverage (0..1) of a pixel by an arc with round caps, antialiased.
static float arc_coverage(const arc_px_t *p, const arc_shape_t *a)
{
    float d;
    if (p->deg >= 0.0f && p->deg <= a->end_deg) {
        d = fabsf(p->r - ARC_RADIUS);
    } else if (p->deg < -a->slack_deg || p->deg > a->end_deg + a->slack_deg) {
        return 0.0f;
    } else {
        float dx0 = p->x - a->cap0_x, dy0 = p->y - a->cap0_y;
        float dx1 = p->x - a->cap1_x, dy1 = p->y - a->cap1_y;
        float d2 = fminf(dx0 * dx0 + dy0 * dy0, dx1 * dx1 + dy1 * dy1);
        d = sqrtf(d2);
    }
    float cov = a->half_width + 0.5f - d;
    return cov <= 0.0f ? 0.0f : (cov >= 1.0f ? 1.0f : cov);
}

static esp_err_t make_ring_layer(void)
{
    const int w = BOARD_LCD_H_RES, h = BOARD_LCD_V_RES;
    const float c = (w - 1) / 2.0f;
    const float edge = w / 2.0f;

    s_ring_px = heap_caps_calloc(w * h, 2, MALLOC_CAP_SPIRAM);
    ESP_RETURN_ON_FALSE(s_ring_px, ESP_ERR_NO_MEM, TAG, "ring image");

    for (int i = 0; i < GLOW_STEPS; i++) {
        glow_color(GLOW_INNER + i / 4.0f, s_glow_base[i]);
    }
    annulus_segments(s_glow_seg, GLOW_INNER, edge);
    band_rects(s_glow_seg, &s_glow_rects);
    int n = count_pixels(s_glow_seg);
    // Read for every pixel on every pulse frame: keep it in internal RAM if it fits.
    s_glow_ridx = heap_caps_malloc(n, MALLOC_CAP_INTERNAL);
    if (!s_glow_ridx) {
        s_glow_ridx = heap_caps_malloc(n, MALLOC_CAP_SPIRAM);
    }
    ESP_RETURN_ON_FALSE(s_glow_ridx, ESP_ERR_NO_MEM, TAG, "glow index");
    int k = 0;
    for (int y = 0; y < h; y++) {
        for (int s = 0; s < 4; s += 2) {
            for (int x = s_glow_seg[y][s]; x < s_glow_seg[y][s + 1]; x++) {
                float r = hypotf(x - c, y - c);
                int i = (int)((r - GLOW_INNER) * 4.0f + 0.5f);
                s_glow_ridx[k++] = i < 0 ? 0 : (i >= GLOW_STEPS ? GLOW_STEPS - 1 : i);
            }
        }
    }

    static segs_t arc_seg;      // 2.9 KB: too big for the caller's stack
    annulus_segments(arc_seg, ARC_INNER, ARC_OUTER + 1);
    band_rects(arc_seg, &s_arc_rects);
    s_arc_px = heap_caps_calloc(count_pixels(arc_seg), sizeof(arc_px_t), MALLOC_CAP_SPIRAM);
    ESP_RETURN_ON_FALSE(s_arc_px, ESP_ERR_NO_MEM, TAG, "arc pixels");
    const arc_shape_t track = arc_shape(ARC_SWEEP_DEG, ARC_WIDTH);
    for (int y = 0; y < h; y++) {
        for (int s = 0; s < 4; s += 2) {
            for (int x = arc_seg[y][s]; x < arc_seg[y][s + 1]; x++) {
                float dx = x - c, dy = y - c;
                float deg = atan2f(dy, dx) * 180.0f / (float)M_PI - ARC_START_DEG;
                while (deg <= -45.0f) {
                    deg += 360.0f;
                }
                arc_px_t *p = &s_arc_px[s_arc_px_count++];
                *p = (arc_px_t){ .idx = y * w + x, .x = dx, .y = dy, .r = hypotf(dx, dy), .deg = deg };
                p->track = (uint8_t)lroundf(255.0f * arc_coverage(p, &track));
            }
        }
    }

    s_ring_dsc.header.magic = LV_IMAGE_HEADER_MAGIC;
    s_ring_dsc.header.cf = LV_COLOR_FORMAT_RGB565;
    s_ring_dsc.header.w = w;
    s_ring_dsc.header.h = h;
    s_ring_dsc.header.stride = w * 2;
    s_ring_dsc.data_size = w * h * 2;
    s_ring_dsc.data = (const uint8_t *)s_ring_px;
    return ESP_OK;
}

// The glow scaled by the LED level, dithered to RGB565. Every pixel at the
// same radius has the same color, so a per-frame table of radius x dither
// phase leaves one lookup per pixel.
static void render_glow(void)
{
    int64_t t0 = esp_timer_get_time();
    static uint16_t lut[GLOW_STEPS][16];   // 6 KB: kept off LVGL's stack
    for (int i = 0; i < GLOW_STEPS; i++) {
        uint32_t r = s_glow_base[i][0] * s_ring_opa / 255;
        uint32_t g = s_glow_base[i][1] * s_ring_opa / 255;
        uint32_t b = s_glow_base[i][2] * s_ring_opa / 255;
        for (int phase = 0; phase < 16; phase++) {
            lut[i][phase] = pack565(r, g, b, dither_threshold(phase & 3, phase >> 2));
        }
    }
    const int w = BOARD_LCD_H_RES;
    const uint8_t *ridx = s_glow_ridx;
    for (int y = 0; y < BOARD_LCD_V_RES; y++) {
        const int16_t *seg = s_glow_seg[y];
        int row_phase = (y & 3) << 2;
        for (int s = 0; s < 4; s += 2) {
            uint16_t *dst = &s_ring_px[y * w + seg[s]];
            for (int x = seg[s]; x < seg[s + 1]; x++) {
                *dst++ = lut[*ridx++][row_phase | (x & 3)];
            }
        }
    }
    g_pm_stats.ui_glow_us = (uint16_t)(esp_timer_get_time() - t0);
    invalidate_band(&s_glow_rects);
}

// Gain arc: the track, and over it the indicator from the start to the gain.
static void render_arc(void)
{
    const arc_state_t *a = &s_arc;
    float frac = 0;
    if (a->visible) {
        frac = (float)(a->gain_tenths - a->min_tenths) / (a->max_tenths - a->min_tenths);
        frac = frac < 0 ? 0 : (frac > 1 ? 1 : frac);
    }
    const arc_shape_t ind = arc_shape(frac * ARC_SWEEP_DEG, a->active ? ARC_WIDTH_ACTIVE : ARC_WIDTH);
    const uint32_t ic = a->active ? COLOR_GAIN_ACTIVE : COLOR_RFHS_RED;
    const int ir = (ic >> 16) & 0xff, ig = (ic >> 8) & 0xff, ib = ic & 0xff;
    const int tr = (COLOR_TRACK >> 16) & 0xff, tg = (COLOR_TRACK >> 8) & 0xff, tb = COLOR_TRACK & 0xff;
    const int w = BOARD_LCD_H_RES;

    for (int i = 0; i < s_arc_px_count; i++) {
        const arc_px_t *p = &s_arc_px[i];
        int track = a->visible ? p->track : 0;
        int cov = (a->visible && frac > 0) ? (int)(255.0f * arc_coverage(p, &ind)) : 0;
        // track over black, then the indicator over that
        int r = tr * track / 255, g = tg * track / 255, b = tb * track / 255;
        r += (ir - r) * cov / 255;
        g += (ig - g) * cov / 255;
        b += (ib - b) * cov / 255;
        s_ring_px[p->idx] = pack565(r, g, b, dither_threshold(p->idx % w, p->idx / w));
    }
    invalidate_band(&s_arc_rects);
}

// ---------------------------------------------------------------------------
// Readout
// ---------------------------------------------------------------------------

static lv_obj_t *make_label(lv_obj_t *parent, const lv_font_t *font, uint32_t color)
{
    lv_obj_t *label = lv_label_create(parent);
    lv_obj_set_style_text_font(label, font, 0);
    lv_obj_set_style_text_color(label, lv_color_hex(color), 0);
    lv_label_set_text(label, "");
    return label;
}

// A horizontal group of labels sharing a bottom line, centered as a whole.
static lv_obj_t *make_row(lv_obj_t *parent)
{
    lv_obj_t *row = lv_obj_create(parent);
    lv_obj_remove_style_all(row);
    lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(row, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_END);
    lv_obj_remove_flag(row, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    return row;
}

static void format_gain(char *buf, size_t len, int tenths)
{
    if (tenths % 10 == 0) {
        snprintf(buf, len, "%d", tenths / 10);
    } else {
        snprintf(buf, len, "%s%d.%d", tenths < 0 ? "-" : "", abs(tenths) / 10, abs(tenths) % 10);
    }
}

// lv_label_set_text() redraws the label even when the text is the same.
static void set_text_if_changed(lv_obj_t *label, const char *text)
{
    if (strcmp(lv_label_get_text(label), text) != 0) {
        lv_label_set_text(label, text);
    }
}

// Shows the readout, or the logo when there are no values (or during the
// splash) and no message. Returns true if the gain arc needs redrawing.
static bool apply_values(const ui_values_t *v, bool splash)
{
    g_pm_stats.display_has_values = v->valid;
    bool readout = v->valid && !splash && s_message_shown == MESSAGE_NONE;
    arc_state_t arc = s_arc;
    arc.visible = readout && v->has_gain && v->gain_max_tenths > v->gain_min_tenths;
    arc.gain_tenths = v->gain_tenths;
    arc.min_tenths = v->gain_min_tenths;
    arc.max_tenths = v->gain_max_tenths;
    bool arc_dirty = memcmp(&arc, &s_arc, sizeof(arc)) != 0;
    s_arc = arc;

    lv_obj_set_flag(s_logo, LV_OBJ_FLAG_HIDDEN, readout || s_message_shown != MESSAGE_NONE);
    if (!readout) {
        lv_obj_add_flag(s_readout, LV_OBJ_FLAG_HIDDEN);
        return arc_dirty;
    }
    char buf[24];
    snprintf(buf, sizeof(buf), "%" PRIu64, v->freq_hz / 1000000);
    set_text_if_changed(s_freq, buf);

    // Fractional MHz to kHz resolution, without trailing zeros.
    uint32_t khz = (uint32_t)((v->freq_hz % 1000000) / 1000);
    if (khz) {
        snprintf(buf, sizeof(buf), ".%03" PRIu32, khz);
        for (char *p = buf + strlen(buf) - 1; *p == '0'; p--) {
            *p = '\0';
        }
        set_text_if_changed(s_freq_frac, buf);
        lv_obj_remove_flag(s_freq_frac, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_freq_frac, LV_OBJ_FLAG_HIDDEN);
    }

    format_gain(buf, sizeof(buf), v->gain_tenths);
    set_text_if_changed(s_gain, buf);
    if (v->has_gain) {
        lv_obj_remove_flag(s_gain_row, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(s_gain_caption, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_gain_row, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(s_gain_caption, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_remove_flag(s_readout, LV_OBJ_FLAG_HIDDEN);
    return arc_dirty;
}

// Holding the screen makes the ring change gain (PowerMate push + rotate),
// so put the emphasis on the gain while the button is down.
static void apply_pressed(bool pressed)
{
    uint32_t freq = pressed ? COLOR_TEXT_DIM : COLOR_TEXT;
    uint32_t gain = pressed ? COLOR_GAIN_ACTIVE : COLOR_GAIN;
    lv_obj_set_style_text_color(s_freq, lv_color_hex(freq), 0);
    lv_obj_set_style_text_color(s_freq_frac, lv_color_hex(freq), 0);
    lv_obj_set_style_text_color(s_gain, lv_color_hex(gain), 0);
    lv_obj_set_style_text_color(s_gain_unit, lv_color_hex(gain), 0);
    lv_obj_set_style_text_color(s_gain_caption, lv_color_hex(pressed ? gain : COLOR_LABEL), 0);
    s_arc.active = pressed;
}

static void show_message(message_t message)
{
    // The board's USB-C port reaches this chip with the plug one way round
    // only; the other way the computer gets the second chip's serial adapter.
    static const char *const text[][3] = {
        [MESSAGE_INVITE] = { LV_SYMBOL_REFRESH, "THIS IS\nINTERACTIVE!",
                             "Twist the knob to\ncontrol the spectrum\nanalyzer display!" },
        [MESSAGE_NO_HOST] = { LV_SYMBOL_USB, "NO USB\nCONNECTION", "Plug in the USB-C cable,\nor flip the plug over" },
        [MESSAGE_FLASH_CONFIRM] = { LV_SYMBOL_DOWNLOAD, "UPDATE\nFIRMWARE?", "Hold the screen\nto allow it" },
    };
    s_message_shown = message;
    g_pm_stats.plug_hint = message == MESSAGE_NO_HOST;
    if (message == MESSAGE_NONE) {
        lv_obj_add_flag(s_message, LV_OBJ_FLAG_HIDDEN);
        return;
    }
    lv_label_set_text(s_message_icon, text[message][0]);
    lv_label_set_text(s_message_title, text[message][1]);
    lv_label_set_text(s_message_body, text[message][2]);
    lv_obj_remove_flag(s_message, LV_OBJ_FLAG_HIDDEN);
}

static ui_screen_t screen_shown(void)
{
    switch (s_message_shown) {
    case MESSAGE_INVITE:
        return UI_SCREEN_INVITE;
    case MESSAGE_NO_HOST:
        return UI_SCREEN_NO_HOST;
    case MESSAGE_FLASH_CONFIRM:
        return UI_SCREEN_FLASH_CONFIRM;
    case MESSAGE_NONE:
        break;
    }
    return lv_obj_has_flag(s_readout, LV_OBJ_FLAG_HIDDEN) ? UI_SCREEN_LOGO : UI_SCREEN_READOUT;
}

static void save_rotation(uint8_t rotation)
{
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_set_u8(nvs, NVS_KEY_ROTATION, rotation);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
}

static void update_timer(lv_timer_t *timer)
{
    static int led_applied = -1;
    static int pressed_applied = -1;
    static bool splash_applied = true;
    static uint32_t fps_window_start;
    bool arc_dirty = false;
    bool glow_dirty = false;

    // A message replaces the readout, so settle it before applying values.
    message_t message = MESSAGE_NONE;
    if (pm_flash_confirm_pending()) {
        message = MESSAGE_FLASH_CONFIRM;
    } else if (atomic_load(&s_no_host_requested)) {
        message = MESSAGE_NO_HOST;
    } else if (led_attract_invite()) {
        message = MESSAGE_INVITE;
    }
    bool message_changed = message != s_message_shown;
    if (message_changed) {
        show_message(message);
    }
    bool splash = esp_timer_get_time() < SPLASH_MS * 1000LL;
    bool splash_changed = splash != splash_applied;
    splash_applied = splash;

    ui_values_t values;
    bool dirty;
    taskENTER_CRITICAL(&s_lock);
    dirty = s_values_dirty || message_changed || splash_changed;
    values = s_values;
    s_values_dirty = false;
    taskEXIT_CRITICAL(&s_lock);
    if (dirty) {
        arc_dirty |= apply_values(&values, splash);
    }
    g_pm_stats.screen = screen_shown();

    int led = atomic_load(&s_led_level);
    if (led != led_applied) {
        // LED level 0 still shows the ring at CONFIG_PM_LED_MIN_PERCENT.
        uint32_t floor = CONFIG_PM_LED_MIN_PERCENT * 255 / 100;
        s_ring_opa = floor + led * (255 - floor) / 255;
        led_applied = led;
        glow_dirty = true;
    }

    int pressed = pm_input_button();
    if (pressed != pressed_applied) {
        apply_pressed(pressed);
        pressed_applied = pressed;
        arc_dirty |= s_arc.visible;
    }

    if (arc_dirty || glow_dirty) {
        cpu_fast(true);                 // released when the refresh is done
    }
    if (arc_dirty) {
        render_arc();
    }
    if (glow_dirty) {
        render_glow();
    }

    int rotation = atomic_exchange(&s_rotation, -1);
    if (rotation >= 0 && rotation != s_rotation_applied) {
        lv_display_set_rotation(s_disp, (lv_display_rotation_t)rotation);
        lv_obj_invalidate(lv_screen_active());
        s_rotation_applied = rotation;
        g_pm_stats.display_rotation = rotation;
        save_rotation(rotation);
    }

    // Rendering needs LVGL's deep stack, so snapshots are taken here.
    if (atomic_exchange(&s_snapshot_requested, false)) {
        if (s_snapshot) {
            lv_draw_buf_destroy(s_snapshot);
        }
        s_snapshot = lv_snapshot_take(lv_screen_active(), LV_COLOR_FORMAT_RGB565);
        xSemaphoreGive(s_snapshot_done);
    }

    uint32_t now = lv_tick_get();
    if (lv_tick_diff(now, fps_window_start) >= 1000) {
        g_pm_stats.ui_fps_x10 = s_refreshes * 10000 / lv_tick_diff(now, fps_window_start);
        s_refreshes = 0;
        fps_window_start = now;
    }
}

static void refresh_start(lv_event_t *e)
{
    cpu_fast(true);
    s_refresh_start = esp_timer_get_time();
}

static void refresh_done(lv_event_t *e)
{
    s_refreshes++;
    g_pm_stats.ui_refresh_ms = (uint16_t)((esp_timer_get_time() - s_refresh_start) / 1000);
    cpu_fast(false);
}

esp_err_t ui_init(lv_display_t *disp)
{
    s_disp = disp;
    esp_pm_lock_create(ESP_PM_CPU_FREQ_MAX, 0, "ui", &s_cpu_lock);
    s_snapshot_done = xSemaphoreCreateBinary();
    ESP_RETURN_ON_FALSE(s_snapshot_done, ESP_ERR_NO_MEM, TAG, "no semaphore");
    ESP_RETURN_ON_ERROR(make_ring_layer(), TAG, "no memory for the ring layer");

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_remove_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

    s_ring = lv_image_create(scr);
    lv_image_set_src(s_ring, &s_ring_dsc);
    lv_obj_center(s_ring);

    // Everything below stays inside ARC_INNER, clear of the ring layer. The
    // logo's corners reach a little further, but the arc is hidden with it.
    s_logo = lv_image_create(scr);
    lv_image_set_src(s_logo, &rfhs_logo);
    lv_obj_center(s_logo);

    s_readout = lv_obj_create(scr);
    lv_obj_remove_style_all(s_readout);
    lv_obj_set_size(s_readout, lv_pct(100), lv_pct(100));
    lv_obj_remove_flag(s_readout, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_readout, LV_OBJ_FLAG_HIDDEN);

    lv_obj_t *freq_row = make_row(s_readout);
    lv_obj_align(freq_row, LV_ALIGN_CENTER, 0, -24);
    s_freq = make_label(freq_row, &font_freq_64, COLOR_TEXT);
    s_freq_frac = make_label(freq_row, &font_value_40, COLOR_TEXT);
    lv_obj_set_style_pad_bottom(s_freq_frac, 1, 0);

    lv_obj_t *unit = make_label(s_readout, &font_caption_18, COLOR_RFHS_CYAN);
    lv_label_set_text(unit, "MHz");
    lv_obj_align(unit, LV_ALIGN_CENTER, 0, 26);

    s_gain_row = make_row(s_readout);
    lv_obj_align(s_gain_row, LV_ALIGN_CENTER, 0, 72);
    s_gain = make_label(s_gain_row, &font_value_40, COLOR_GAIN);
    s_gain_unit = make_label(s_gain_row, &font_caption_18, COLOR_GAIN);
    lv_label_set_text(s_gain_unit, " dB");
    lv_obj_set_style_pad_bottom(s_gain_unit, 3, 0);

    s_gain_caption = make_label(s_readout, &font_caption_18, COLOR_LABEL);
    lv_label_set_text(s_gain_caption, "GAIN");
    lv_obj_align(s_gain_caption, LV_ALIGN_CENTER, 0, 104);

    s_message = lv_obj_create(scr);
    lv_obj_remove_style_all(s_message);
    lv_obj_set_size(s_message, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(s_message, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(s_message, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_row(s_message, 10, 0);
    lv_obj_remove_flag(s_message, LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_center(s_message);
    lv_obj_add_flag(s_message, LV_OBJ_FLAG_HIDDEN);
    s_message_icon = make_label(s_message, &lv_font_montserrat_32, COLOR_RFHS_CYAN);
    s_message_title = make_label(s_message, &font_title_22, COLOR_TEXT);
    s_message_body = make_label(s_message, &font_label_22, COLOR_HINT);
    lv_obj_set_style_text_align(s_message_title, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_align(s_message_body, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_line_space(s_message_title, 4, 0);
    lv_obj_set_style_text_line_space(s_message_body, 3, 0);

    apply_pressed(false);
    g_pm_stats.screen = UI_SCREEN_LOGO;   // the splash
    s_ring_opa = 0x80;      // the PowerMate's default level, until update_timer() runs
    render_glow();
    render_arc();

    uint8_t rotation = CONFIG_PM_DISPLAY_ROTATION;
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) == ESP_OK) {
        nvs_get_u8(nvs, NVS_KEY_ROTATION, &rotation);
        nvs_close(nvs);
    }
    rotation &= 3;
    lv_display_set_rotation(disp, (lv_display_rotation_t)rotation);
    s_rotation_applied = rotation;
    g_pm_stats.display_rotation = rotation;

    lv_display_add_event_cb(disp, refresh_start, LV_EVENT_REFR_START, NULL);
    lv_display_add_event_cb(disp, refresh_done, LV_EVENT_REFR_READY, NULL);
    lv_timer_create(update_timer, UPDATE_PERIOD_MS, NULL);
    return ESP_OK;
}

void ui_set_values(const ui_values_t *values)
{
    taskENTER_CRITICAL(&s_lock);
    s_values = *values;
    s_values_dirty = true;
    taskEXIT_CRITICAL(&s_lock);
}

void ui_get_values(ui_values_t *values)
{
    taskENTER_CRITICAL(&s_lock);
    *values = s_values;
    taskEXIT_CRITICAL(&s_lock);
}

void ui_set_led(uint8_t level)
{
    atomic_store(&s_led_level, level);
}

void ui_set_no_host(bool show)
{
    atomic_store(&s_no_host_requested, show);
}

void ui_set_rotation(uint8_t quarter_turns)
{
    atomic_store(&s_rotation, quarter_turns & 3);
}

esp_err_t ui_snapshot_take(ui_snapshot_info_t *info)
{
    if (!s_snapshot_done) {
        return ESP_ERR_INVALID_STATE;
    }
    xSemaphoreTake(s_snapshot_done, 0);     // clear one left from a request that timed out
    atomic_store(&s_snapshot_requested, true);
    if (xSemaphoreTake(s_snapshot_done, pdMS_TO_TICKS(2000)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }
    ESP_RETURN_ON_FALSE(s_snapshot, ESP_ERR_NO_MEM, TAG, "snapshot failed");

    info->width = s_snapshot->header.w;
    info->height = s_snapshot->header.h;
    info->stride = s_snapshot->header.stride;
    info->color_format = s_snapshot->header.cf;
    info->size = s_snapshot->data_size;
    return ESP_OK;
}

const uint8_t *ui_snapshot_data(size_t *size)
{
    *size = s_snapshot ? s_snapshot->data_size : 0;
    return s_snapshot ? s_snapshot->data : NULL;
}
