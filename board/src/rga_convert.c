/**
 * rga_convert.c — RGA 硬件加速格式转换与旋转
 * NV12 ↔ RGB888，使用 librga im2d API（RK3588）。
 */
#include "rga_convert.h"

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <rga/im2d.h>
#include <rga/rga.h>

int rga_nv12_to_rgb(const void *nv12, void *rgb, int width, int height)
{
    if (!nv12 || !rgb || width <= 0 || height <= 0) {
        fprintf(stderr, "[rga] nv12_to_rgb: invalid param\n");
        return -1;
    }

    rga_buffer_t src = wrapbuffer_virtualaddr(
        (void *)nv12, width, height, RK_FORMAT_YCbCr_420_SP);
    rga_buffer_t dst = wrapbuffer_virtualaddr(
        rgb, width, height, RK_FORMAT_RGB_888);

    src.width  = width;  src.height  = height;
    src.wstride = width; src.hstride = height;
    dst.width  = width;  dst.height  = height;
    dst.wstride = width; dst.hstride = height;

    imsetColorSpace(&src, IM_YUV_TO_RGB_BT601_FULL);
    imsetColorSpace(&dst, IM_YUV_TO_RGB_BT601_FULL);

    IM_STATUS ret = imcvtcolor(src, dst,
                               RK_FORMAT_YCbCr_420_SP,
                               RK_FORMAT_RGB_888,
                               IM_YUV_TO_RGB_BT601_FULL);
    if (ret != IM_STATUS_SUCCESS && ret != IM_STATUS_NOERROR) {
        fprintf(stderr, "[rga] imcvtcolor failed: %d\n", (int)ret);
        return -1;
    }
    return 0;
}

int rga_rgb_to_nv12(const void *rgb, void *nv12, int width, int height)
{
    if (!rgb || !nv12 || width <= 0 || height <= 0 ||
        (width & 1) || (height & 1)) {
        fprintf(stderr, "[rga] rgb_to_nv12: invalid param\n");
        return -1;
    }

    rga_buffer_t src = wrapbuffer_virtualaddr(
        (void *)rgb, width, height, RK_FORMAT_RGB_888);
    rga_buffer_t dst = wrapbuffer_virtualaddr(
        nv12, width, height, RK_FORMAT_YCbCr_420_SP);

    src.width = width; src.height = height;
    src.wstride = width; src.hstride = height;
    dst.width = width; dst.height = height;
    dst.wstride = width; dst.hstride = height;

    IM_STATUS ret = imcvtcolor(src, dst,
                               RK_FORMAT_RGB_888,
                               RK_FORMAT_YCbCr_420_SP,
                               IM_RGB_TO_YUV_BT601_FULL);
    if (ret != IM_STATUS_SUCCESS && ret != IM_STATUS_NOERROR) {
        fprintf(stderr, "[rga] imcvtcolor RGB->NV12 failed: %d\n", (int)ret);
        return -1;
    }
    return 0;
}

int rga_nv12_to_rgb_resize(const void *nv12, int src_w, int src_h,
                           void *rgb,        int dst_w, int dst_h)
{
    if (!nv12 || !rgb || src_w <= 0 || src_h <= 0 || dst_w <= 0 || dst_h <= 0) {
        fprintf(stderr, "[rga] nv12_to_rgb_resize: invalid param\n");
        return -1;
    }
    if (src_w == dst_w && src_h == dst_h)
        return rga_nv12_to_rgb(nv12, rgb, src_w, src_h);

    size_t src_rgb_size = (size_t)src_w * src_h * 3;
    void *rgb_tmp = malloc(src_rgb_size);
    if (!rgb_tmp) {
        fprintf(stderr, "[rga] resize: malloc %zu failed\n", src_rgb_size);
        return -1;
    }
    if (rga_nv12_to_rgb(nv12, rgb_tmp, src_w, src_h) != 0) {
        free(rgb_tmp);
        return -1;
    }

    rga_buffer_t src_buf = wrapbuffer_virtualaddr(
        rgb_tmp, src_w, src_h, RK_FORMAT_RGB_888);
    rga_buffer_t dst_buf = wrapbuffer_virtualaddr(
        rgb, dst_w, dst_h, RK_FORMAT_RGB_888);
    src_buf.width  = src_w;  src_buf.height  = src_h;
    src_buf.wstride = src_w; src_buf.hstride = src_h;
    dst_buf.width  = dst_w;  dst_buf.height  = dst_h;
    dst_buf.wstride = dst_w; dst_buf.hstride = dst_h;

    IM_STATUS ret = imresize(src_buf, dst_buf);
    if (ret != IM_STATUS_SUCCESS && ret != IM_STATUS_NOERROR) {
        fprintf(stderr, "[rga] imresize failed: %d\n", (int)ret);
        free(rgb_tmp);
        return -1;
    }
    free(rgb_tmp);
    return 0;
}

int rga_nv12_to_rgb_rot(const void *nv12, void *rgb,
                        int src_w, int src_h,
                        int dst_w, int dst_h, int dst_wstride,
                        int rot_deg)
{
    if (!nv12 || !rgb || src_w <= 0 || src_h <= 0 || dst_w <= 0 || dst_h <= 0) {
        fprintf(stderr, "[rga] nv12_to_rgb_rot: invalid param\n");
        return -1;
    }

    rga_buffer_t src = wrapbuffer_virtualaddr((void *)nv12, src_w, src_h, RK_FORMAT_YCbCr_420_SP);
    rga_buffer_t dst = wrapbuffer_virtualaddr(rgb, dst_w, dst_h, RK_FORMAT_RGB_888);

    src.width = src_w; src.height = src_h; src.wstride = src_w; src.hstride = src_h;
    dst.width = dst_w; dst.height = dst_h; dst.wstride = dst_wstride; dst.hstride = dst_h;

    imsetColorSpace(&src, IM_YUV_TO_RGB_BT601_FULL);
    imsetColorSpace(&dst, IM_YUV_TO_RGB_BT601_FULL);

    int rot_mode = 0;
    if (rot_deg == 90) rot_mode = IM_HAL_TRANSFORM_ROT_90;
    else if (rot_deg == 180) rot_mode = IM_HAL_TRANSFORM_ROT_180;
    else if (rot_deg == 270) rot_mode = IM_HAL_TRANSFORM_ROT_270;

    IM_STATUS ret;
    if (rot_mode == 0) {
        ret = imcvtcolor(src, dst, RK_FORMAT_YCbCr_420_SP, RK_FORMAT_RGB_888, IM_YUV_TO_RGB_BT601_FULL);
    } else {
        ret = imrotate(src, dst, rot_mode);
    }

    if (ret != IM_STATUS_SUCCESS && ret != IM_STATUS_NOERROR) {
        fprintf(stderr, "[rga] nv12_to_rgb_rot (rot=%d) failed: %d\n", rot_deg, (int)ret);
        return -1;
    }
    return 0;
}

int rga_rgb_to_nv12_stride(const void *rgb, void *nv12,
                           int width, int height, int wstride)
{
    if (!rgb || !nv12 || width <= 0 || height <= 0 || (width & 1) || (height & 1)) {
        fprintf(stderr, "[rga] rgb_to_nv12_stride: invalid param\n");
        return -1;
    }

    rga_buffer_t src = wrapbuffer_virtualaddr((void *)rgb, width, height, RK_FORMAT_RGB_888);
    rga_buffer_t dst = wrapbuffer_virtualaddr(nv12, width, height, RK_FORMAT_YCbCr_420_SP);

    src.width = width; src.height = height; src.wstride = wstride; src.hstride = height;
    dst.width = width; dst.height = height; dst.wstride = wstride; dst.hstride = height;

    IM_STATUS ret = imcvtcolor(src, dst, RK_FORMAT_RGB_888, RK_FORMAT_YCbCr_420_SP, IM_RGB_TO_YUV_BT601_FULL);
    if (ret != IM_STATUS_SUCCESS && ret != IM_STATUS_NOERROR) {
        fprintf(stderr, "[rga] rgb_to_nv12_stride failed: %d\n", (int)ret);
        return -1;
    }
    return 0;
}

int rga_nv12_rot(const void *src_nv12, void *dst_nv12,
                 int src_w, int src_h,
                 int dst_w, int dst_h, int dst_wstride,
                 int rot_deg)
{
    if (!src_nv12 || !dst_nv12 || src_w <= 0 || src_h <= 0 || dst_w <= 0 || dst_h <= 0) {
        fprintf(stderr, "[rga] nv12_rot: invalid param\n");
        return -1;
    }

    rga_buffer_t src = wrapbuffer_virtualaddr((void *)src_nv12, src_w, src_h, RK_FORMAT_YCbCr_420_SP);
    rga_buffer_t dst = wrapbuffer_virtualaddr(dst_nv12, dst_w, dst_h, RK_FORMAT_YCbCr_420_SP);

    src.width = src_w; src.height = src_h; src.wstride = src_w; src.hstride = src_h;
    dst.width = dst_w; dst.height = dst_h; dst.wstride = dst_wstride; dst.hstride = dst_h;

    int rot_mode = 0;
    if (rot_deg == 90) rot_mode = IM_HAL_TRANSFORM_ROT_90;
    else if (rot_deg == 180) rot_mode = IM_HAL_TRANSFORM_ROT_180;
    else if (rot_deg == 270) rot_mode = IM_HAL_TRANSFORM_ROT_270;

    IM_STATUS ret;
    if (rot_mode == 0) {
        ret = imcopy(src, dst);
    } else {
        ret = imrotate(src, dst, rot_mode);
    }

    if (ret != IM_STATUS_SUCCESS && ret != IM_STATUS_NOERROR) {
        fprintf(stderr, "[rga] nv12_rot (rot=%d) failed: %d\n", rot_deg, (int)ret);
        return -1;
    }
    return 0;
}
