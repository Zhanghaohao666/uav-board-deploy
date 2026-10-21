/**
 * tracker_stub.c — 检测跟踪算法接入点（空实现）
 *
 * 当前什么都不做，直接返回，等价于"不接算法、纯透传 RGB"。
 * 算法同事接入时，把处理逻辑写在这里（或在这里调用你们的库），
 * 在 rgb 上原地画框/叠加即可，无需改动 streamer.c。
 */
#include "tracker_stub.h"

void tracker_process(uint8_t *rgb, int width, int height, int cam_id)
{
    (void)rgb;
    (void)width;
    (void)height;
    (void)cam_id;

    /* ───────────────── 算法接入点 ─────────────────
     * 示例（伪代码）:
     *   detections = your_detect(rgb, width, height);
     *   track = your_track(detections, cam_id);
     *   draw_box(rgb, width, height, track.box);
     * ──────────────────────────────────────────── */
}
