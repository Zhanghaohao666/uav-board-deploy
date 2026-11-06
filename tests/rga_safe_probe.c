/* Native RK3588 regression: image planes, padding, cache lifetime and timing.
 * Synthetic buffers only; no camera or listening video port is used. */
#include "rga_safe.h"
#include "rga_convert.h"
#include <rga/im2d.h>
#include <rga/rga.h>
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static int descriptors(void)
{
    DIR *dir = opendir("/proc/self/fd");
    if (!dir) return -1;
    int n = 0;
    while (readdir(dir)) ++n;
    closedir(dir);
    return n;
}
static double clock_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000.0 + t.tv_nsec / 1000000.0;
}
static void check(int okay, const char *name)
{
    if (!okay) { fprintf(stderr, "FAIL %s\n", name); exit(2); }
}
static void gray_nv12(unsigned char *p, int width, int height, int y)
{
    memset(p, y, (size_t)width * height);
    memset(p + (size_t)width * height, 128, (size_t)width * height / 2);
}
int main(void)
{
    const int width = 1920, height = 1080, low_w = 640, low_h = 360;
    size_t full = (size_t)width * height * 3 / 2;
    size_t small = (size_t)low_w * low_h * 3 / 2;
    unsigned char *src = malloc(full), *dst = malloc(small + 32);
    unsigned char *rgb = malloc((size_t)width * height * 3);
    unsigned char *nv12 = malloc(full);
    check(src && dst && rgb && nv12, "allocation");
    gray_nv12(src, width, height, 80);
    memset(dst, 0xaa, small + 32);
    UavRgaLayout input = {width, height, width, height, RK_FORMAT_YCbCr_420_SP};
    UavRgaLayout output = {low_w, low_h, low_w, low_h, RK_FORMAT_YCbCr_420_SP};
    check(uav_rga_run(src, input, dst, output, UAV_RGA_RESIZE, 0, 0) == 0, "resize 1080 -> 360");
    for (size_t i = 0; i < small; ++i)
        check(abs((int)dst[i] - (i < (size_t)low_w * low_h ? 80 : 128)) <= 1,
              "visible NV12 Y/UV values including final row");
    for (size_t i = small; i < small + 32; ++i) check(dst[i] == 0xaa, "output canary");
    check(rga_nv12_to_rgb(src, rgb, width, height) == 0, "HD NV12 -> RGB");
    for (size_t i = 0; i < (size_t)width * height * 3; ++i)
        check(abs((int)rgb[i] - 80) <= 3, "full-range RGB pixels");
    check(rga_rgb_to_nv12(rgb, nv12, width, height) == 0, "HD RGB -> NV12");
    for (size_t i = 0; i < full; ++i)
        check(abs((int)nv12[i] - (i < (size_t)width * height ? 80 : 128)) <= 4,
              "full-range NV12 round trip");
    /* A non-square pattern checks the visible orientation after rotation. */
    gray_nv12(src, width, height, 80);
    for (int y = 0; y < height / 2; ++y) memset(src + (size_t)y * width, 160, width / 2);
    unsigned char *rot = malloc((size_t)1088 * 1920 * 3);
    check(rot != NULL, "rotation allocation");
    for (int deg = 90; deg <= 270; deg += 90) {
        int dw = deg == 180 ? width : height;
        int dh = deg == 180 ? height : width;
        int stride = (dw + 15) & ~15;
        check(rga_nv12_to_rgb_rot(src, rot, width, height, dw, dh, stride, deg) == 0,
              "rotated HD RGB");
        int x = deg == 90 || deg == 180 ? dw * 3 / 4 : dw / 4;
        int y = deg == 180 || deg == 270 ? dh * 3 / 4 : dh / 4;
        check(abs((int)rot[((size_t)y * stride + x) * 3] - 160) <= 3,
              "rotation quadrant");
    }
    free(rot);
    gray_nv12(src, width, height, 80);
    uav_rga_release_thread();
    int baseline = descriptors();
    double begin = clock_ms();
    for (int cycle = 0; cycle < 8; ++cycle) {
        for (int frame = 0; frame < 30; ++frame)
            check(uav_rga_run(src, input, dst, output, UAV_RGA_RESIZE, 0, 0) == 0,
                  "repeated lease frames");
        uav_rga_release_thread();
        check(descriptors() == baseline, "DMA32 and handle descriptors released on idle");
    }
    double elapsed = clock_ms() - begin;
    printf("{\"ok\":true,\"frames\":240,\"lease_cycles\":8,\"resize_ms\":%.3f,\"fd_baseline\":%d}\n",
           elapsed / 240.0, baseline);
    free(src); free(dst); free(rgb); free(nv12);
    return 0;
}
