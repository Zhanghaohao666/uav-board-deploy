/**
 * mpp_encoder.c — RK3588 MPP 硬件视频编码实现
 *
 * 标准 MPP MPI 编码流程：
 *   mpp_create → mpp_init(MPP_CTX_ENC) → mpp_enc_cfg_* → control(SET_CFG)
 *   每帧: 拷贝到 DMA buffer → encode_put_frame → encode_get_packet
 *
 * 输入帧按相机原生紧凑排布（无行填充）；编码器要求 stride 16 对齐，
 * 因此内部按行 memcpy 到对齐的 DMA buffer。
 */
#include "mpp_encoder.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MODULE_TAG "rk3588_streamer"

#include <rockchip/rk_mpi.h>
#include <rockchip/mpp_buffer.h>
#include <rockchip/mpp_frame.h>
#include <rockchip/mpp_packet.h>

#define MPP_ALIGN(x, a) (((x) + (a) - 1) & ~((a) - 1))

struct MppEncoder {
    MppCtx          ctx;
    MppApi         *mpi;
    MppEncCfg       cfg;
    MppBufferGroup  grp;
    MppBuffer       frm_buf;     /* DMA 输入帧缓冲 */
    EncConfig       conf;

    int  hor_stride;            /* 字节 */
    int  ver_stride;            /* 行 */
    size_t frame_size;          /* DMA buffer 大小 */
    MppFrameFormat mpp_fmt;

    uint8_t *outbuf;            /* 拷出已编码数据 */
    size_t   outcap;
};

MppEncoder *enc_create(const EncConfig *cfg)
{
    MppEncoder *e = (MppEncoder *)calloc(1, sizeof(*e));
    if (!e) return NULL;
    e->conf = *cfg;

    int w = cfg->width, h = cfg->height;

    if (cfg->in_fmt == ENC_FMT_RGB888) {
        e->mpp_fmt    = MPP_FMT_RGB888;
        e->hor_stride = MPP_ALIGN(w * 3, 16);          /* RGB: 字节 stride */
        e->ver_stride = MPP_ALIGN(h, 16);
        e->frame_size = (size_t)e->hor_stride * e->ver_stride;
    } else {
        e->mpp_fmt    = MPP_FMT_YUV420SP;              /* NV12 */
        e->hor_stride = MPP_ALIGN(w, 16);              /* luma 字节 stride */
        e->ver_stride = MPP_ALIGN(h, 16);
        e->frame_size = (size_t)e->hor_stride * e->ver_stride * 3 / 2;
    }

    MPP_RET ret = mpp_create(&e->ctx, &e->mpi);
    if (ret) { fprintf(stderr, "[enc] mpp_create failed %d\n", ret); goto fail; }

    MppCodingType coding = (cfg->codec == ENC_CODEC_H265)
                         ? MPP_VIDEO_CodingHEVC : MPP_VIDEO_CodingAVC;
    ret = mpp_init(e->ctx, MPP_CTX_ENC, coding);
    if (ret) { fprintf(stderr, "[enc] mpp_init failed %d\n", ret); goto fail; }

    ret = mpp_enc_cfg_init(&e->cfg);
    if (ret) { fprintf(stderr, "[enc] cfg_init failed %d\n", ret); goto fail; }

    /* prep */
    mpp_enc_cfg_set_s32(e->cfg, "prep:width",      w);
    mpp_enc_cfg_set_s32(e->cfg, "prep:height",     h);
    mpp_enc_cfg_set_s32(e->cfg, "prep:hor_stride", (cfg->in_fmt == ENC_FMT_RGB888)
                                                   ? e->hor_stride / 3 : e->hor_stride);
    mpp_enc_cfg_set_s32(e->cfg, "prep:ver_stride", e->ver_stride);
    mpp_enc_cfg_set_s32(e->cfg, "prep:format",     e->mpp_fmt);

    /* rate control */
    int bps     = cfg->bitrate_kbps * 1000;
    int bps_max = (cfg->bitrate_max_kbps > 0 ? cfg->bitrate_max_kbps * 1000
                                             : bps * 3 / 2);
    int rc_mode = (cfg->rc_mode == ENC_RC_CBR)
                  ? MPP_ENC_RC_MODE_CBR : MPP_ENC_RC_MODE_VBR;
    mpp_enc_cfg_set_s32(e->cfg, "rc:mode", rc_mode);
    if (rc_mode == MPP_ENC_RC_MODE_CBR) {
        /* 恒定码率：上下波动收窄 */
        mpp_enc_cfg_set_s32(e->cfg, "rc:bps_target", bps);
        mpp_enc_cfg_set_s32(e->cfg, "rc:bps_max",    bps * 17 / 16);
        mpp_enc_cfg_set_s32(e->cfg, "rc:bps_min",    bps * 15 / 16);
    } else {
        /* VBR 质量优先：运动时冲到 max，静态时降到 min 省带宽 */
        mpp_enc_cfg_set_s32(e->cfg, "rc:bps_target", bps);
        mpp_enc_cfg_set_s32(e->cfg, "rc:bps_max",    bps_max);
        mpp_enc_cfg_set_s32(e->cfg, "rc:bps_min",    bps / 4);
    }
    mpp_enc_cfg_set_s32(e->cfg, "rc:fps_in_flex", 0);
    mpp_enc_cfg_set_s32(e->cfg, "rc:fps_in_num",  cfg->fps);
    mpp_enc_cfg_set_s32(e->cfg, "rc:fps_in_denorm",  1);
    mpp_enc_cfg_set_s32(e->cfg, "rc:fps_out_flex", 0);
    mpp_enc_cfg_set_s32(e->cfg, "rc:fps_out_num", cfg->fps);
    mpp_enc_cfg_set_s32(e->cfg, "rc:fps_out_denorm", 1);
    mpp_enc_cfg_set_s32(e->cfg, "rc:gop",         cfg->gop > 0 ? cfg->gop : cfg->fps * 2);

    /* codec */
    mpp_enc_cfg_set_s32(e->cfg, "codec:type", coding);
    if (coding == MPP_VIDEO_CodingAVC) {
        /* High Profile, Level 4.0, CABAC */
        mpp_enc_cfg_set_s32(e->cfg, "h264:profile",   100);
        mpp_enc_cfg_set_s32(e->cfg, "h264:level",     40);
        mpp_enc_cfg_set_s32(e->cfg, "h264:cabac_en",  1);
        mpp_enc_cfg_set_s32(e->cfg, "h264:cabac_idc", 0);
        mpp_enc_cfg_set_s32(e->cfg, "h264:trans8x8",  1);
    } else {
        mpp_enc_cfg_set_s32(e->cfg, "h265:profile", 1);   /* main */
        mpp_enc_cfg_set_s32(e->cfg, "h265:level",   120);
    }

    ret = e->mpi->control(e->ctx, MPP_ENC_SET_CFG, e->cfg);
    if (ret) { fprintf(stderr, "[enc] SET_CFG failed %d\n", ret); goto fail; }

    /* 每个 IDR 都带 SPS/PPS，方便接收端中途加入 */
    MppEncHeaderMode hdr_mode = MPP_ENC_HEADER_MODE_EACH_IDR;
    e->mpi->control(e->ctx, MPP_ENC_SET_HEADER_MODE, &hdr_mode);

    /* 分配 DMA 输入缓冲 */
    ret = mpp_buffer_group_get_internal(&e->grp, MPP_BUFFER_TYPE_DRM);
    if (ret) { fprintf(stderr, "[enc] buffer_group failed %d\n", ret); goto fail; }
    ret = mpp_buffer_get(e->grp, &e->frm_buf, e->frame_size);
    if (ret) { fprintf(stderr, "[enc] buffer_get failed %d\n", ret); goto fail; }

    e->outcap = e->frame_size;      /* 输出一定小于一帧原始大小 */
    e->outbuf = (uint8_t *)malloc(e->outcap);
    if (!e->outbuf) goto fail;

    printf("[enc] %s %dx%d stride=%d/%d fmt=%s %s %dkbps(max%d) gop=%d ok\n",
           coding == MPP_VIDEO_CodingHEVC ? "H265" : "H264",
           w, h, e->hor_stride, e->ver_stride,
           cfg->in_fmt == ENC_FMT_RGB888 ? "RGB888" : "NV12",
           rc_mode == MPP_ENC_RC_MODE_CBR ? "CBR" : "VBR",
           cfg->bitrate_kbps, bps_max / 1000,
           cfg->gop > 0 ? cfg->gop : cfg->fps * 2);
    return e;

fail:
    enc_destroy(e);
    return NULL;
}

/* 把紧凑排布的源帧按行拷贝到对齐的 DMA buffer */
static void copy_to_dma(MppEncoder *e, const void *src, size_t len)
{
    uint8_t *dst = (uint8_t *)mpp_buffer_get_ptr(e->frm_buf);
    const uint8_t *s = (const uint8_t *)src;
    int w = e->conf.width, h = e->conf.height;

    if (e->conf.in_fmt == ENC_FMT_RGB888) {
        int src_stride = w * 3;
        int dst_stride = e->hor_stride;
        if (src_stride == dst_stride || len >= e->frame_size) {
            memcpy(dst, s, (size_t)dst_stride * h);
        } else {
            for (int r = 0; r < h; r++)
                memcpy(dst + r * dst_stride, s + r * src_stride, src_stride);
        }
    } else {
        int dst_stride = e->hor_stride;
        const uint8_t *src_y  = s;
        const uint8_t *src_uv = s + (size_t)w * h;
        uint8_t *dst_y  = dst;
        uint8_t *dst_uv = dst + (size_t)dst_stride * e->ver_stride;
        if (len >= e->frame_size) {
            /* Full NV12 buffer already aligned to hor_stride * ver_stride (e.g. from RGA) */
            memcpy(dst, s, e->frame_size);
        } else if (dst_stride == w) {
            memcpy(dst_y, src_y, (size_t)w * h);
            memcpy(dst_uv, src_uv, (size_t)w * (h / 2));
        } else {
            for (int r = 0; r < h; r++)
                memcpy(dst_y + r * dst_stride, src_y + r * w, w);
            for (int r = 0; r < h / 2; r++)
                memcpy(dst_uv + r * dst_stride, src_uv + r * w, w);
        }
    }
}

static int annexb_is_keyframe(const uint8_t *d, size_t n, int is_h265)
{
    for (size_t i = 0; i + 4 < n; i++) {
        if (d[i] == 0 && d[i+1] == 0 &&
            ((d[i+2] == 1) || (d[i+2] == 0 && d[i+3] == 1))) {
            size_t h = (d[i+2] == 1) ? i + 3 : i + 4;
            if (h >= n) break;
            uint8_t b = d[h];
            if (is_h265) {
                int t = (b >> 1) & 0x3F;
                if ((t >= 16 && t <= 23) || t == 32 || t == 33 || t == 34) return 1;
            } else {
                int t = b & 0x1F;
                if (t == 5 || t == 7) return 1;
            }
        }
    }
    return 0;
}

int enc_encode(MppEncoder *e, const void *data, size_t len,
               const uint8_t **out, size_t *out_len, int *is_key)
{
    if (!e || !data || !out || !out_len) return -1;
    const size_t required =
        (e->conf.in_fmt == ENC_FMT_RGB888)
            ? (size_t)e->conf.width * e->conf.height * 3
            : (size_t)e->conf.width * e->conf.height * 3 / 2;
    if (len < required) {
        fprintf(stderr, "[enc] short input frame: %zu < %zu bytes\n",
                len, required);
        return -1;
    }
    copy_to_dma(e, data, len);

    MppFrame frame = NULL;
    if (mpp_frame_init(&frame)) return -1;
    mpp_frame_set_width(frame,  e->conf.width);
    mpp_frame_set_height(frame, e->conf.height);
    mpp_frame_set_hor_stride(frame, (e->conf.in_fmt == ENC_FMT_RGB888)
                                    ? e->hor_stride / 3 : e->hor_stride);
    mpp_frame_set_ver_stride(frame, e->ver_stride);
    mpp_frame_set_fmt(frame, e->mpp_fmt);
    mpp_frame_set_eos(frame, 0);
    mpp_frame_set_buffer(frame, e->frm_buf);

    if (e->mpi->encode_put_frame(e->ctx, frame)) {
        fprintf(stderr, "[enc] put_frame failed\n");
        mpp_frame_deinit(&frame);
        return -1;
    }
    mpp_frame_deinit(&frame);

    MppPacket packet = NULL;
    if (e->mpi->encode_get_packet(e->ctx, &packet) || !packet) {
        fprintf(stderr, "[enc] get_packet failed\n");
        return -1;
    }

    void  *ptr = mpp_packet_get_pos(packet);
    size_t plen = mpp_packet_get_length(packet);
    if (plen > e->outcap) {
        uint8_t *nb = (uint8_t *)realloc(e->outbuf, plen);
        if (!nb) { mpp_packet_deinit(&packet); return -1; }
        e->outbuf = nb; e->outcap = plen;
    }
    memcpy(e->outbuf, ptr, plen);

    if (is_key)
        *is_key = annexb_is_keyframe(e->outbuf, plen,
                                     e->conf.codec == ENC_CODEC_H265);
    mpp_packet_deinit(&packet);

    *out     = e->outbuf;
    *out_len = plen;
    return 0;
}

void enc_request_keyframe(MppEncoder *e)
{
    if (e && e->mpi)
        e->mpi->control(e->ctx, MPP_ENC_SET_IDR_FRAME, NULL);
}

void enc_destroy(MppEncoder *e)
{
    if (!e) return;
    if (e->mpi && e->ctx) e->mpi->reset(e->ctx);
    if (e->frm_buf) mpp_buffer_put(e->frm_buf);
    if (e->grp)     mpp_buffer_group_put(e->grp);
    if (e->cfg)     mpp_enc_cfg_deinit(e->cfg);
    if (e->ctx)     mpp_destroy(e->ctx);
    if (e->outbuf)  free(e->outbuf);
    free(e);
}
