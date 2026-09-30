#pragma once
#include <stddef.h>
#include <stdint.h>

/* 640x512 measurement model: each row is RAW16[640] + UYVY[640].
 * Capture has 520 rows; the last 8 are metadata, not image pixels. */
int mino_unpack_nv12(const uint8_t *src, size_t bytes, size_t stride,
                     uint8_t *dst, size_t capacity, int rotate180);
