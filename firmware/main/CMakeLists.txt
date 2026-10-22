idf_component_register(SRCS "main.c"
                            "display.c"
                            "encoder.c"
                            "haptic.c"
                            "i2c_bus.c"
                            "led.c"
                            "pm_state.c"
                            "touch.c"
                            "ui.c"
                            "usb_powermate.c"
                            "fonts/font_freq_64.c"
                            "fonts/font_value_40.c"
                            "fonts/font_title_22.c"
                            "fonts/font_caption_18.c"
                            "fonts/font_label_22.c"
                            "images/rfhs_logo.c"
                       INCLUDE_DIRS "." "tusb")

# TinyUSB is configured by our tusb/tusb_config.h
idf_component_get_property(tusb_lib espressif__tinyusb COMPONENT_LIB)
target_include_directories(${tusb_lib} PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/tusb")
