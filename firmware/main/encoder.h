#pragma once

#include "esp_err.h"

// Start sampling the rotary ring; detents are fed to pm_input_add_rotation().
esp_err_t encoder_init(void);
