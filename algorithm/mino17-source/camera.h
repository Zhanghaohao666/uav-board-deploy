#pragma once
/**
 * camera.h — v4l2 多路相机采集封装
 * 支持 NV12 / YUYV 格式 mmap 采集，适配 RK3588 rkcif/rkisp 管线。
 * 可创建多个 Camera 实例，分别采集不同的 /dev/videoN。
 */

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CAM_MAX_BUFS 4

typedef struct {
    void   *start;   /* mmap 映射地址 */
    size_t  length;  /* 缓冲区字节数  */
} CamBuf;

typedef struct {
    int      fd;
    int      width;
    int      height;
    unsigned pixfmt;     /* V4L2_PIX_FMT_NV12 / V4L2_PIX_FMT_YUYV ... */
    int      use_mplane;
    int      buf_count;
    uint64_t last_timestamp_us; /* 最近一次 DQBUF 的 V4L2 时间戳 */
    uint32_t last_sequence;
    uint32_t last_flags;
    CamBuf   bufs[CAM_MAX_BUFS];
} Camera;

/**
 * cam_open  — 打开设备，申请 mmap 缓冲区，启动流
 * 返回 0 成功，-1 失败（设备不存在/未插相机时返回 -1，调用方可跳过）
 */
int cam_open(Camera *cam, const char *dev,
             int width, int height,
             unsigned pixfmt, int buf_count, int fps);

/**
 * cam_grab  — 取一帧（阻塞直到有帧可用，内部 select 超时 2s）
 * @data_out : 输出帧数据指针（指向 mmap 区域，无需释放）
 * @len_out  : 输出帧字节数
 * @buf_idx  : 输出本次使用的缓冲区下标（归还时需要）
 * 返回 0 成功，-1 表示瞬时错误，-2 表示设备掉线/需要重新打开。
 */
int cam_grab(Camera *cam, void **data_out, size_t *len_out, int *buf_idx);

/** cam_release — 将缓冲区归还给驱动（grab 之后必须调用） */
int cam_release(Camera *cam, int buf_idx);

/** cam_close — 停流、释放 mmap、关闭 fd */
void cam_close(Camera *cam);

#ifdef __cplusplus
}
#endif
