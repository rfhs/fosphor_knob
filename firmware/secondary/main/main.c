// Firmware for the Waveshare knob's second chip (ESP32-U4WDH): keep it asleep.
//
// Its factory firmware runs Bluetooth audio and HID (it advertises as
// "TAIJI_KNOB_HID" / "TAIJI_KNOB_AUDIO"), which the PowerMate emulator on the
// main chip doesn't use: an open radio on the demo table, and tens of mA. This
// mutes the audio DAC and puts the chip into deep sleep with its radios never
// started. It only wakes on reset, which the CH340 does when reflashing.

#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "esp_sleep.h"

#define DAC_XSMT GPIO_NUM_32   // PCM5100A soft mute, low = muted

void app_main(void)
{
    // Keep the DAC muted through sleep so the speaker can't pick up noise
    // from the I2S lines floating.
    gpio_reset_pin(DAC_XSMT);
    gpio_set_direction(DAC_XSMT, GPIO_MODE_OUTPUT);
    gpio_set_level(DAC_XSMT, 0);
    rtc_gpio_hold_en(DAC_XSMT);

    esp_deep_sleep_start();     // no wake-up source: until the next reset
}
