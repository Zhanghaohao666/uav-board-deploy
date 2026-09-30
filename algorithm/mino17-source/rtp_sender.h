#pragma once
/**
 * rtp_sender.h — RTP over UDP 发送器
 *
 * 将 H.264 / H.265 的 Annex-B 裸流按 RFC 6184 (H264) / RFC 7798 (H265)
 * 打包为 RTP 包，经 UDP 发往对端板子。
 *
 * 同时提供一个 raw（不压缩）UDP 发送接口，用于原生 YUV/RGB 帧的分片传输
 * （仅建议低分辨率 / 单路使用，整帧会被分片为多个 UDP 包）。
 */
#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct RtpSender RtpSender;

/* payload_h265=0 时按 H264 打包，=1 时按 H265 打包 */
RtpSender *rtp_open(const char *dst_ip, int dst_port, int payload_h265);

/**
 * rtp_send_annexb — 发送一个编码帧（Annex-B，可能含多个 NAL）
 * @ts90k : 该帧的 90kHz 时间戳
 */
int rtp_send_annexb(RtpSender *s, const uint8_t *annexb, size_t len, uint32_t ts90k);

void rtp_close(RtpSender *s);

/* ---- raw 不压缩传输（自定义分片协议，配套 recv 端解析）---- */
typedef struct RawSender RawSender;
RawSender *raw_open(const char *dst_ip, int dst_port);
/* 发送一整帧原始数据，带 8 字节帧头(magic,seq,w,h,fmt,total_len) + 分片 */
int  raw_send_frame(RawSender *s, const void *data, size_t len,
                    uint16_t w, uint16_t h, uint8_t fmt);
void raw_close(RawSender *s);

#ifdef __cplusplus
}
#endif
