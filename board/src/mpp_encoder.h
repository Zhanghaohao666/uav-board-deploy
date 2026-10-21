#pragma once
/**
 * mpp_encoder.h — RK3588 MPP 硬件视频编码封装
 * 支持 H.264 / H.265，输入 NV12(YUV420SP) 或 RGB888（硬件内部做 CSC）。
 * 编码输出为 Annex-B 裸流（带起始码 00 00 00 01），可直接送 RTP 打包。
 */
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    ENC_CODEC_H264 = 0,
    ENC_CODEC_H265 = 1,
} EncCodec;

typedef enum {
    ENC_FMT_NV12   = 0,   /* YUV420SP，相机原生 */
    ENC_FMT_RGB888 = 1,   /* RGA 转换后的 RGB */
} EncFmt;

typedef enum {
    ENC_RC_VBR = 0,       /* 质量优先：按需给码率，静态画面自动省带宽（推荐） */
    ENC_RC_CBR = 1,       /* 恒定码率：码率最稳定，但静态画面也填满 */
} EncRcMode;

typedef struct MppEncoder MppEncoder;

typedef struct {
    int       width;
    int       height;
    EncFmt    in_fmt;
    EncCodec  codec;
    EncRcMode rc_mode;
    int       fps;
    int       bitrate_kbps;       /* 目标码率 */
    int       bitrate_max_kbps;   /* 码率上限；<=0 时自动取 target*3/2 */
    int       gop;                /* I 帧间隔，建议 = 2*fps */
} EncConfig;

/** 创建并初始化编码器，返回句柄，失败返回 NULL */
MppEncoder *enc_create(const EncConfig *cfg);

/**
 * enc_encode — 编码一帧
 * @data    : 输入帧数据（NV12 或 RGB888，紧凑排布，无行填充）
 * @len     : 输入字节数
 * @out      : 输出已编码 Annex-B 数据指针（指向内部缓冲，下次调用前有效）
 * @out_len  : 输出字节数
 * @is_key   : 输出本帧是否为关键帧（含 SPS/PPS）
 * 返回 0 成功，-1 失败
 */
int enc_encode(MppEncoder *e, const void *data, size_t len,
               const uint8_t **out, size_t *out_len, int *is_key);

/** 强制下一帧为 IDR 关键帧（新接收端加入时调用） */
void enc_request_keyframe(MppEncoder *e);

void enc_destroy(MppEncoder *e);

#ifdef __cplusplus
}
#endif
