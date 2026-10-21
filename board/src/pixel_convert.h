#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

int yuyv_to_nv12(const void *yuyv, size_t input_bytes,
                 void *nv12, int width, int height);

int z16_to_pseudocolor_rgb(const void *z16, size_t input_bytes,
                           void *rgb, int width, int height,
                           int depth_min_mm, int depth_max_mm);

#ifdef __cplusplus
}
#endif
