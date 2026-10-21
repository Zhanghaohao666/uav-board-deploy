#pragma once
/**
 * tracker_stub.h — 检测跟踪算法接入点（桩）
 *
 * 流水线位置:
 *   v4l2(NV12) → RGA(NV12→RGB888) → [tracker_process] → MPP 编码 → RTP 推流
 *
 * 算法同事只需实现 tracker_process()：
 *   - 输入/输出都是同一块 RGB888 buffer（紧凑排布，每像素 3 字节，BGR? 见说明）
 *   - 原地处理：在画面上画框/叠加跟踪结果，直接改写 rgb 内存即可
 *   - 处理后的 rgb 会被继续送去编码推流
 *
 * 注意:
 *   - 本函数在每路相机的采集线程里、每帧调用一次，要尽量快（别阻塞太久，
 *     否则会拖低该路帧率）。耗时的推理建议内部用 RKNN/NPU 异步处理。
 *   - rgb 格式与 /data/rk3588_tracker 的 rga_nv12_to_rgb 输出一致（RGB888）。
 */
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * tracker_process — 检测跟踪处理（当前为空桩，原样返回）
 * @rgb    : RGB888 数据指针（in/out，原地修改）
 * @width  : 图像宽
 * @height : 图像高
 * @cam_id : 相机/流编号（0,1,2...，便于每路维护各自的跟踪状态）
 */
void tracker_process(uint8_t *rgb, int width, int height, int cam_id);

#ifdef __cplusplus
}
#endif
