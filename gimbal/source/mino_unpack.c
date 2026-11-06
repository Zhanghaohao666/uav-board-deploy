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

/* HIK 2bdf:0102 / 640x1032: 512 raw rows + 4 info rows +
 * 512 YUYV image rows + 4 info rows. Metadata/raw never enters preview. */
int hik_unpack_nv12(const unsigned char *src, size_t src_len, size_t stride,
                    unsigned char *dst, size_t dst_len, int rotate180) {
    const size_t w=640,h=512,offset=516;
    if (!src || !dst || stride < w*2 || stride > SIZE_MAX/(offset+h) ||
        src_len < stride*(offset+h) || dst_len < w*h*3/2 ||
        (rotate180!=0 && rotate180!=1)) return -1;
    for (size_t y=0;y<h;y++) {
        const unsigned char *p=src+(offset+y)*stride;
        size_t dy=rotate180 ? h-1-y : y;
        for (size_t x=0;x<w;x+=2) {
            dst[dy*w+(rotate180?w-1-x:x)]=p[2*x];
            dst[dy*w+(rotate180?w-2-x:x+1)]=p[2*x+2];
        }
        if (!(y&1)) {
            const unsigned char *q=p+stride;
            size_t uvrow=rotate180 ? h/2-1-y/2 : y/2;
            for(size_t x=0;x<w;x+=2) {
                size_t uv=w*h+uvrow*w+(rotate180?w-2-x:x);
                dst[uv]=(unsigned char)(((unsigned)p[2*x+1]+q[2*x+1]+1)/2);
                dst[uv+1]=(unsigned char)(((unsigned)p[2*x+3]+q[2*x+3]+1)/2);
            }
        }
    }
    return 0;
}
