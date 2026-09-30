#include "mino_unpack.h"

int mino_unpack_nv12(const uint8_t *src, size_t bytes, size_t stride,
                     uint8_t *dst, size_t capacity, int rotate180)
{
    const size_t w = 640, h = 512, offset = 1280;
    if (!src || !dst || stride < 2560 || bytes < 511 * stride + 2560 ||
        capacity < w * h * 3 / 2) return -1;
    for (size_t y = 0; y < h; ++y) {
        const uint8_t *row = src + y * stride + offset;
        for (size_t x = 0; x < w; ++x) {
            size_t pos = y * w + x;
            dst[rotate180 ? w * h - 1 - pos : pos] = row[2 * x + 1];
        }
    }
    for (size_t y = 0; y < h; y += 2) {
        const uint8_t *a = src + y * stride + offset;
        const uint8_t *b = a + stride;
        for (size_t x = 0; x < w; x += 2) {
            size_t pos = (y / 2) * w + x;
            if (rotate180) pos = w * h / 2 - 2 - pos;
            dst[w * h + pos] = (uint8_t)(((unsigned)a[2*x] + b[2*x] + 1) / 2);
            dst[w * h + pos + 1] = (uint8_t)(((unsigned)a[2*x+2] + b[2*x+2] + 1) / 2);
        }
    }
    return 0;
}
