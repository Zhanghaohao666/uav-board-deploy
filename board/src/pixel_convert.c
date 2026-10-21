#include "pixel_convert.h"

#include <stdlib.h>

static uint8_t clamp_u8(int value)
{
    if (value < 0) return 0;
    if (value > 255) return 255;
    return (uint8_t)value;
}

int yuyv_to_nv12(const void *yuyv_ptr, size_t input_bytes,
                 void *nv12_ptr, int width, int height)
{
    size_t required = (size_t)width * height * 2;
    if (!yuyv_ptr || !nv12_ptr || width <= 0 || height <= 0 ||
        (width & 1) || (height & 1) || input_bytes < required) {
        return -1;
    }

    const uint8_t *src = (const uint8_t *)yuyv_ptr;
    uint8_t *y_plane = (uint8_t *)nv12_ptr;
    uint8_t *uv_plane = y_plane + (size_t)width * height;

    for (int y = 0; y < height; ++y) {
        const uint8_t *row = src + (size_t)y * width * 2;
        uint8_t *dst_y = y_plane + (size_t)y * width;
        for (int x = 0; x < width; x += 2) {
            dst_y[x] = row[x * 2];
            dst_y[x + 1] = row[x * 2 + 2];
        }
    }

    for (int y = 0; y < height; y += 2) {
        const uint8_t *row0 = src + (size_t)y * width * 2;
        const uint8_t *row1 = row0 + (size_t)width * 2;
        uint8_t *dst_uv = uv_plane + (size_t)(y / 2) * width;
        for (int x = 0; x < width; x += 2) {
            int offset = x * 2;
            dst_uv[x] = (uint8_t)(((int)row0[offset + 1] +
                                   (int)row1[offset + 1] + 1) / 2);
            dst_uv[x + 1] = (uint8_t)(((int)row0[offset + 3] +
                                       (int)row1[offset + 3] + 1) / 2);
        }
    }
    return 0;
}

int z16_to_pseudocolor_rgb(const void *z16_ptr, size_t input_bytes,
                           void *rgb_ptr, int width, int height,
                           int depth_min_mm, int depth_max_mm)
{
    size_t pixels = (size_t)width * height;
    if (!z16_ptr || !rgb_ptr || width <= 0 || height <= 0 ||
        input_bytes < pixels * sizeof(uint16_t) ||
        depth_min_mm < 0 || depth_max_mm <= depth_min_mm) {
        return -1;
    }

    const uint16_t *depth = (const uint16_t *)z16_ptr;
    uint8_t *rgb = (uint8_t *)rgb_ptr;
    int range = depth_max_mm - depth_min_mm;

    for (size_t i = 0; i < pixels; ++i) {
        int value = depth[i];
        uint8_t *out = rgb + i * 3;
        if (value == 0) {
            out[0] = out[1] = out[2] = 0;
            continue;
        }
        if (value < depth_min_mm) value = depth_min_mm;
        if (value > depth_max_mm) value = depth_max_mm;
        int position = (depth_max_mm - value) * 1023 / range;
        out[0] = clamp_u8(384 - abs(position - 768));
        out[1] = clamp_u8(384 - abs(position - 512));
        out[2] = clamp_u8(384 - abs(position - 256));
    }
    return 0;
}
