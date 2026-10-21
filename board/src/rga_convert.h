#pragma once
/**
 * rga_convert.h — RGA 硬件加速格式转换与旋转封装
 * NV12 (YUV420SP) ↔ RGB888，使用 librga。
 */

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

int rga_nv12_to_rgb(const void *nv12, void *rgb, int width, int height);
int rga_rgb_to_nv12(const void *rgb, void *nv12, int width, int height);
int rga_nv12_to_rgb_resize(const void *nv12, int src_w, int src_h,
                           void *rgb,        int dst_w, int dst_h);

/**
 * rga_nv12_to_rgb_rot - NV12 转换到 RGB888，并执行硬件旋转 (0, 90, 180, 270)
 * @nv12: 输入 NV12 (src_w x src_h, stride=src_w)
 * @rgb:  输出 RGB888 (dst_w x dst_h, stride=dst_wstride)
 * @src_w, @src_h: 原始采集宽高 (如 1920x1080)
 * @dst_w, @dst_h: 旋转后宽高 (如 1080x1920)
 * @dst_wstride:  输出宽度 stride (16对齐，如 1088)
 * @rot_deg:      旋转角度: 0, 90, 180, 270
 */
int rga_nv12_to_rgb_rot(const void *nv12, void *rgb,
                        int src_w, int src_h,
                        int dst_w, int dst_h, int dst_wstride,
                        int rot_deg);

/**
 * rga_rgb_to_nv12_stride - RGB888 转换为 NV12，支持自定义 stride
 */
int rga_rgb_to_nv12_stride(const void *rgb, void *nv12,
                           int width, int height, int wstride);

/**
 * rga_nv12_rot - NV12 硬件旋转 (0, 90, 180, 270)
 * @src_nv12: 输入 NV12 (src_w x src_h, stride=src_w)
 * @dst_nv12: 输出 NV12 (dst_w x dst_h, stride=dst_wstride)
 */
int rga_nv12_rot(const void *src_nv12, void *dst_nv12,
                 int src_w, int src_h,
                 int dst_w, int dst_h, int dst_wstride,
                 int rot_deg);

#ifdef __cplusplus
}
#endif
