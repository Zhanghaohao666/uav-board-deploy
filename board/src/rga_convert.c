/** RGA conversions through imported DMA32 buffers; no kernel userptr calls. */
#include "rga_convert.h"
#include "rga_safe.h"
#include <stdlib.h>
#include <rga/im2d.h>
#include <rga/rga.h>

static UavRgaLayout layout(int w, int h, int stride, int format)
{
    return (UavRgaLayout){w, h, stride, h, format};
}
static int rotation(int degrees)
{
    switch (degrees) {
    case 90: return IM_HAL_TRANSFORM_ROT_90;
    case 180: return IM_HAL_TRANSFORM_ROT_180;
    case 270: return IM_HAL_TRANSFORM_ROT_270;
    default: return 0;
    }
}
int rga_nv12_to_rgb(const void *nv12, void *rgb, int width, int height)
{
    return uav_rga_run(nv12, layout(width, height, width, RK_FORMAT_YCbCr_420_SP),
                       rgb, layout(width, height, width, RK_FORMAT_RGB_888),
                       UAV_RGA_COLOR, 0, IM_YUV_TO_RGB_BT601_FULL);
}
int rga_rgb_to_nv12(const void *rgb, void *nv12, int width, int height)
{
    return uav_rga_run(rgb, layout(width, height, width, RK_FORMAT_RGB_888),
                       nv12, layout(width, height, width, RK_FORMAT_YCbCr_420_SP),
                       UAV_RGA_COLOR, 0, IM_RGB_TO_YUV_BT601_FULL);
}
int rga_nv12_to_rgb_resize(const void *nv12, int src_w, int src_h,
                           void *rgb, int dst_w, int dst_h)
{
    if (!nv12 || !rgb || src_w <= 0 || src_h <= 0 || src_w > 8192 || src_h > 8192 ||
        dst_w <= 0 || dst_h <= 0 || dst_w > 8192 || dst_h > 8192) return -1;
    if (src_w == dst_w && src_h == dst_h)
        return rga_nv12_to_rgb(nv12, rgb, src_w, src_h);
    void *temporary = malloc((size_t)src_w * src_h * 3);
    if (!temporary) return -1;
    int rc = rga_nv12_to_rgb(nv12, temporary, src_w, src_h);
    if (!rc) rc = uav_rga_run(temporary, layout(src_w, src_h, src_w, RK_FORMAT_RGB_888),
                              rgb, layout(dst_w, dst_h, dst_w, RK_FORMAT_RGB_888),
                              UAV_RGA_RESIZE, 0, IM_COLOR_SPACE_DEFAULT);
    free(temporary);
    return rc;
}
int rga_nv12_to_rgb_rot(const void *nv12, void *rgb,
                        int src_w, int src_h, int dst_w, int dst_h,
                        int dst_wstride, int rot_deg)
{
    if (rot_deg != 0 && !rotation(rot_deg)) return -1;
    return uav_rga_run(nv12, layout(src_w, src_h, src_w, RK_FORMAT_YCbCr_420_SP),
                       rgb, layout(dst_w, dst_h, dst_wstride, RK_FORMAT_RGB_888),
                       rot_deg ? UAV_RGA_ROTATE : UAV_RGA_COLOR, rotation(rot_deg),
                       IM_YUV_TO_RGB_BT601_FULL);
}
int rga_rgb_to_nv12_stride(const void *rgb, void *nv12,
                           int width, int height, int wstride)
{
    return uav_rga_run(rgb, layout(width, height, wstride, RK_FORMAT_RGB_888),
                       nv12, layout(width, height, wstride, RK_FORMAT_YCbCr_420_SP),
                       UAV_RGA_COLOR, 0, IM_RGB_TO_YUV_BT601_FULL);
}
int rga_nv12_rot(const void *src_nv12, void *dst_nv12,
                 int src_w, int src_h, int dst_w, int dst_h,
                 int dst_wstride, int rot_deg)
{
    if (rot_deg != 0 && !rotation(rot_deg)) return -1;
    return uav_rga_run(src_nv12, layout(src_w, src_h, src_w, RK_FORMAT_YCbCr_420_SP),
                       dst_nv12, layout(dst_w, dst_h, dst_wstride, RK_FORMAT_YCbCr_420_SP),
                       rot_deg ? UAV_RGA_ROTATE : UAV_RGA_COPY, rotation(rot_deg),
                       IM_COLOR_SPACE_DEFAULT);
}
