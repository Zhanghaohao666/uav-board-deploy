/**
 * streamer.c — RK3588 多路 MIPI 相机网络推流主程序
 *
 * 管线（每路相机一个线程）:
 *   v4l2(NV12) ──┬─ format=yuv ─────────────────► MPP 编码(NV12)
 *                └─ format=rgb ─ RGA(NV12→RGB888) ► 算法 ► RGA(RGB888→NV12)
 *                                                        ► MPP 编码(NV12)
 *                                                        └► RTP/UDP ──► 对端板子
 *
 * 也支持 --transport raw：不压缩，整帧分片经 UDP 发送（仅建议低码率/单路）。
 *
 * 预留接口：未插的 MIPI（如第三路）可照常用 --cam 指定，打开失败会自动跳过并定时重试。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <signal.h>
#include <pthread.h>
#include <getopt.h>
#include <time.h>
#include <linux/videodev2.h>

#ifndef V4L2_PIX_FMT_Z16
#define V4L2_PIX_FMT_Z16 v4l2_fourcc('Z', '1', '6', ' ')
#endif

#include "camera.h"
#include "rga_convert.h"
#include "pixel_convert.h"
#include "mpp_encoder.h"
#include "rtp_sender.h"
#include "tracker_stub.h"
#include "low_stream.h"

#define MAX_CAMS 8

typedef enum { FMT_YUV = 0, FMT_RGB = 1 } PixMode;
typedef enum { TR_RTP = 0, TR_RAW = 1 } Transport;
typedef enum { INPUT_NV12 = 0, INPUT_YUYV = 1, INPUT_Z16 = 2 } InputFormat;

typedef struct {
    char     dev[256];
    int      width;
    int      height;
    int      port;          /* 目标 UDP 端口（这一路） */
    /* 每路可选覆盖；-2 表示"沿用全局"（bitrate 用 -2 区别于 0=自动） */
    int      fps;
    int      bitrate;
    int      codec;         /* EncCodec 或 -2 */
    int      fmt;           /* PixMode 或 -2 */
    int      rc;            /* EncRcMode 或 -2 */
    int      gop;
    int      input;
    int      depth_min_mm;
    int      depth_max_mm;
    int      rot;           /* 旋转角度: 0, 90, 180, 270 */
    char     host[64];      /* 每路可选覆盖目标 IP；空=沿用全局 --host */
} CamSpec;

typedef struct {
    CamSpec  spec;
    /* 全局参数副本 */
    char     host[64];
    PixMode  fmt;
    Transport transport;
    EncCodec codec;
    EncRcMode rc_mode;
    int      fps;
    int      bitrate;       /* <=0 表示按分辨率自动 */
    int      gop;
    InputFormat input;
    int      depth_min_mm;
    int      depth_max_mm;
    int      rot;
    int      idx;
} ThreadArg;

static volatile sig_atomic_t g_running = 1;
static volatile sig_atomic_t g_camera_fault = 0;
static void on_sig(int s) { (void)s; g_running = 0; }

static void request_camera_restart(int stream_index, const char *reason)
{
    fprintf(stderr, "[stream %d] %s; requesting process restart\n",
            stream_index, reason);
    g_camera_fault = 1;
    g_running = 0;
}

/* 按分辨率给“质量优先又不浪费”的默认目标码率(kbps) */
static int auto_bitrate(int w, int h)
{
    long px = (long)w * h;
    if (px <= 640 * 480)   return 3000;    /* <=VGA   */
    if (px <= 1280 * 720)  return 6000;    /* <=720p  */
    if (px <= 1920 * 1080) return 10000;   /* <=1080p */
    if (px <= 2560 * 1440) return 16000;   /* <=2K    */
    return 24000;                          /* 4K 级   */
}

static uint64_t now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000ull + ts.tv_nsec / 1000;
}

#define USE_GLOBAL (-2)

/* 解析单路可选参数串，形如 "fps=30,bitrate=6000,codec=h265,format=rgb,rc=vbr,gop=60,host=1.2.3.4"
 * 未出现的项保持 USE_GLOBAL（沿用全局默认）。返回 0 成功。 */
static int parse_cam_opts(CamSpec *cs, char *opts)
{
    char *save = NULL;
    for (char *tok = strtok_r(opts, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
        char *eq = strchr(tok, '=');
        if (!eq) { fprintf(stderr, "每路参数应为 key=value: %s\n", tok); return -1; }
        *eq = 0;
        const char *k = tok, *v = eq + 1;
        if      (!strcmp(k, "fps"))     cs->fps     = atoi(v);
        else if (!strcmp(k, "bitrate")) cs->bitrate = atoi(v);
        else if (!strcmp(k, "gop"))     cs->gop     = atoi(v);
        else if (!strcmp(k, "codec"))   cs->codec   = !strcmp(v, "h265") ? ENC_CODEC_H265 : ENC_CODEC_H264;
        else if (!strcmp(k, "format"))  cs->fmt     = !strcmp(v, "rgb")  ? FMT_RGB : FMT_YUV;
        else if (!strcmp(k, "rc"))      cs->rc      = !strcmp(v, "cbr")  ? ENC_RC_CBR : ENC_RC_VBR;
        else if (!strcmp(k, "host"))    snprintf(cs->host, sizeof(cs->host), "%s", v);
        else if (!strcmp(k, "input")) {
            if (!strcmp(v, "yuyv")) cs->input = INPUT_YUYV;
            else if (!strcmp(v, "z16")) cs->input = INPUT_Z16;
            else if (!strcmp(v, "nv12")) cs->input = INPUT_NV12;
            else { fprintf(stderr, "未知输入格式: %s\n", v); return -1; }
        }
        else if (!strcmp(k, "depth_min")) cs->depth_min_mm = atoi(v);
        else if (!strcmp(k, "depth_max")) cs->depth_max_mm = atoi(v);
        else if (!strcmp(k, "rot"))       cs->rot = atoi(v);
        else { fprintf(stderr, "未知每路参数: %s\n", k); return -1; }
    }
    return 0;
}

/* 单路相机采集→(可选RGA)→编码→推流 */
static void *cam_thread(void *p)
{
    ThreadArg *a = (ThreadArg *)p;
    const char *dev = a->spec.dev;
    int W = a->spec.width, H = a->spec.height;
    unsigned capture_pixfmt = V4L2_PIX_FMT_NV12;
    if (a->input == INPUT_YUYV) capture_pixfmt = V4L2_PIX_FMT_YUYV;
    if (a->input == INPUT_Z16) capture_pixfmt = V4L2_PIX_FMT_Z16;

    /* 打开相机；失败则定时重试（预留口/未插相机场景） */
    Camera cam;
    int open_failures = 0;
    /* MIPI/NV12 实时流只保留双缓冲，避免处理抖动时积压旧帧。
       USB/D455 仍保留 4 个缓冲，优先保证设备稳定性。 */
    int capture_buffers = (a->input == INPUT_NV12) ? 2 : 4;
    while (g_running) {
        if (cam_open(&cam, dev, W, H, capture_pixfmt,
                     capture_buffers, a->fps) == 0) break;
        ++open_failures;
        fprintf(stderr, "[stream %d] %s 打开失败，5s 后重试（可能未插相机/预留口）\n",
                a->idx, dev);
        if (a->input != INPUT_NV12 && open_failures >= 3) {
            request_camera_restart(a->idx,
                                   "D455 open failed three times");
            return NULL;
        }
        for (int i = 0; i < 50 && g_running; i++) usleep(100000);
    }
    if (!g_running) return NULL;
    W = cam.width; H = cam.height;   /* 用驱动实际值 */
    int rot = a->rot;
    int out_w = W, out_h = H;
    if (rot == 90 || rot == 270) {
        out_w = H;
        out_h = W;
    }
    int out_wstride = (out_w + 15) & ~15;
    int out_hstride = out_h;

    /* 所有输入最终归一化为紧凑 NV12 后送入 MPP。 */
    uint8_t *rgb = NULL;
    uint8_t *padded_rgb = NULL;
    uint8_t *encoded_nv12 = NULL;
    uint8_t *rotated_nv12 = NULL;

    if (a->fmt == FMT_RGB || a->input == INPUT_Z16) {
        rgb = (uint8_t *)malloc((size_t)out_w * out_h * 3);
        if (rot != 0) {
            padded_rgb = (uint8_t *)malloc((size_t)out_wstride * out_hstride * 3);
        } else {
            padded_rgb = rgb;
        }
        if (!rgb || !padded_rgb) {
            fprintf(stderr, "[stream %d] rgb malloc 失败\n", a->idx);
            cam_close(&cam);
            return NULL;
        }
    }
    if (a->fmt == FMT_RGB || a->input != INPUT_NV12) {
        encoded_nv12 = (uint8_t *)malloc((size_t)out_wstride * out_hstride * 3 / 2);
        if (!encoded_nv12) {
            fprintf(stderr, "[stream %d] encoded_nv12 malloc 失败\n", a->idx);
            if (rot != 0 && padded_rgb) free(padded_rgb);
            if (rgb) free(rgb);
            cam_close(&cam);
            return NULL;
        }
    }
    if (a->fmt == FMT_YUV && rot != 0) {
        rotated_nv12 = (uint8_t *)malloc((size_t)out_wstride * out_hstride * 3 / 2);
        if (!rotated_nv12) {
            fprintf(stderr, "[stream %d] rotated_nv12 malloc 失败\n", a->idx);
            cam_close(&cam);
            return NULL;
        }
    }

    /* 编码器（raw 模式不需要） */
    MppEncoder *enc = NULL;
    RtpSender  *rtp = NULL;
    RawSender  *raw = NULL;
    LowStream  *low = NULL;

    if (a->transport == TR_RTP) {
        int br = (a->bitrate > 0) ? a->bitrate : auto_bitrate(out_w, out_h);
        EncConfig ec = {
            .width = out_w, .height = out_h,
            /* 算法可以使用 RGB，但编码统一输入 NV12，兼容 RK3588 MPP。 */
            .in_fmt = ENC_FMT_NV12,
            .codec = a->codec, .rc_mode = a->rc_mode, .fps = a->fps,
            .bitrate_kbps = br, .bitrate_max_kbps = 0, .gop = a->gop,
        };
        enc = enc_create(&ec);
        if (!enc) { fprintf(stderr, "[stream %d] 编码器创建失败\n", a->idx); goto cleanup; }
        rtp = rtp_open(a->host, a->spec.port, a->codec == ENC_CODEC_H265);
        if (!rtp) goto cleanup;
        low = low_stream_create(a->host, a->spec.port, out_w, out_h);
    } else {
        raw = raw_open(a->host, a->spec.port);
        if (!raw) goto cleanup;
    }

    const char *input_name = a->input == INPUT_YUYV ? "YUYV" :
                             a->input == INPUT_Z16 ? "Z16" : "NV12";
    printf("[stream %d] %s %dx%d(out %dx%d rot=%d) input=%s processing=%s transport=%s port=%d 开始推流\n",
           a->idx, dev, W, H, out_w, out_h, rot, input_name, a->fmt == FMT_RGB ? "RGB" : "YUV",
           a->transport == TR_RTP ? "RTP" : "RAW", a->spec.port);

    uint64_t t0 = now_us();
    long frames = 0;
    uint64_t stat_t = t0;
    uint64_t next_encode_deadline_us = 0;
    uint64_t capture_age_sum_us = 0, capture_age_max_us = 0;
    uint64_t process_sum_us = 0, process_max_us = 0;
    uint64_t encode_sum_us = 0, encode_max_us = 0;
    uint64_t send_sum_us = 0, send_max_us = 0;
    uint64_t latency_samples = 0;
    int consecutive_grab_failures = 0;
    int consecutive_short_frames = 0;
    /* 按目标 fps 限速：相机可能以 60/120fps 出帧，多余帧直接丢弃，
       既不浪费编码算力，也不浪费网络带宽（码率稳定在目标值） */
    uint64_t frame_interval = (a->fps > 0) ? (1000000ull / a->fps) : 0;

    while (g_running) {
        void *data; size_t len; int bi;
        int grab_result = cam_grab(&cam, &data, &len, &bi);
        if (grab_result == -2) {
            if (a->input != INPUT_NV12) {
                request_camera_restart(a->idx, "D455 device I/O error");
                break;
            }
            fprintf(stderr, "[stream %d] %s 设备掉线，等待重新连接\n",
                    a->idx, dev);
            cam_close(&cam);
            while (g_running) {
                sleep(1);
                if (cam_open(&cam, dev, W, H, capture_pixfmt,
                             capture_buffers,
                             a->fps) == 0) {
                    if (cam.width == W && cam.height == H) {
                        fprintf(stderr, "[stream %d] %s 已重新连接\n",
                                a->idx, dev);
                        break;
                    }
                    fprintf(stderr,
                            "[stream %d] %s 重连分辨率不匹配: %dx%d\n",
                            a->idx, dev, cam.width, cam.height);
                    cam_close(&cam);
                }
            }
            continue;
        }
        if (grab_result != 0) {
            if (a->input != INPUT_NV12 &&
                ++consecutive_grab_failures >= 3) {
                request_camera_restart(a->idx,
                                       "D455 capture failed three times");
                break;
            }
            usleep(5000);
            continue;
        }
        consecutive_grab_failures = 0;

        const size_t capture_required =
            (a->input == INPUT_NV12)
                ? (size_t)W * H * 3 / 2
                : (size_t)W * H * 2;
        if (len < capture_required) {
            fprintf(stderr,
                    "[stream %d] short %s frame: %zu < %zu bytes\n",
                    a->idx,
                    a->input == INPUT_YUYV ? "YUYV" :
                    a->input == INPUT_Z16 ? "Z16" : "NV12",
                    len, capture_required);
            cam_release(&cam, bi);
            if (a->input != INPUT_NV12 &&
                ++consecutive_short_frames >= 30) {
                request_camera_restart(a->idx,
                                       "D455 produced 30 short frames");
                break;
            }
            continue;
        }
        consecutive_short_frames = 0;

        /* 基于固定时间轴限速，避免 60->30fps 时因轻微抖动误降到 20fps。 */
        uint64_t now = now_us();
        if (frame_interval && next_encode_deadline_us &&
            now + 2000 < next_encode_deadline_us) {
            cam_release(&cam, bi);
            continue;
        }
        if (frame_interval) {
            if (!next_encode_deadline_us)
                next_encode_deadline_us = now;
            do {
                next_encode_deadline_us += frame_interval;
            } while (next_encode_deadline_us <= now);
        }
        uint64_t pipeline_start_us = now_us();
        uint64_t capture_age_us = 0;
        if ((cam.last_flags & V4L2_BUF_FLAG_TIMESTAMP_MASK) ==
                V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC &&
            pipeline_start_us >= cam.last_timestamp_us) {
            capture_age_us = pipeline_start_us - cam.last_timestamp_us;
        }

        const void *nv12_frame = data;
        const void *frame_in = data;
        uint8_t in_fmt_raw = 0;        /* raw 协议: 0=NV12 1=RGB */
        size_t frame_bytes = (size_t)W * H * 3 / 2;

        if (a->input == INPUT_YUYV) {
            if (yuyv_to_nv12(data, len, encoded_nv12, W, H) != 0) {
                fprintf(stderr, "[stream %d] YUYV 转 NV12 失败，bytes=%zu\n",
                        a->idx, len);
                cam_release(&cam, bi);
                continue;
            }
            nv12_frame = encoded_nv12;
        } else if (a->input == INPUT_Z16) {
            if (z16_to_pseudocolor_rgb(data, len, rgb, W, H,
                                       a->depth_min_mm,
                                       a->depth_max_mm) != 0 ||
                rga_rgb_to_nv12(rgb, encoded_nv12, W, H) != 0) {
                fprintf(stderr, "[stream %d] Z16 伪彩转换失败，bytes=%zu\n",
                        a->idx, len);
                cam_release(&cam, bi);
                continue;
            }
            nv12_frame = encoded_nv12;
        }
        frame_in = nv12_frame;

        if (a->fmt == FMT_RGB && a->input != INPUT_Z16) {
            if (rot != 0) {
                if (rga_nv12_to_rgb_rot(nv12_frame, padded_rgb, W, H, out_w, out_h, out_wstride, rot) != 0) {
                    cam_release(&cam, bi);
                    continue;
                }
                for (int y = 0; y < out_h; ++y) {
                    memcpy(rgb + (size_t)y * out_w * 3,
                           padded_rgb + (size_t)y * out_wstride * 3,
                           (size_t)out_w * 3);
                }
            } else {
                if (rga_nv12_to_rgb(nv12_frame, rgb, W, H) != 0) {
                    cam_release(&cam, bi);
                    continue;
                }
            }

            /* ★ 检测跟踪接入点：在已旋转正的 RGB888 上原地处理/画框 */
            tracker_process(rgb, out_w, out_h, a->idx);
            in_fmt_raw = 1;

            if (a->transport == TR_RTP) {
                if (rot != 0) {
                    for (int y = 0; y < out_h; ++y) {
                        memcpy(padded_rgb + (size_t)y * out_wstride * 3,
                               rgb + (size_t)y * out_w * 3,
                               (size_t)out_w * 3);
                    }
                    if (rga_rgb_to_nv12_stride(padded_rgb, encoded_nv12, out_w, out_h, out_wstride) != 0) {
                        cam_release(&cam, bi);
                        continue;
                    }
                } else {
                    if (rga_rgb_to_nv12(rgb, encoded_nv12, W, H) != 0) {
                        cam_release(&cam, bi);
                        continue;
                    }
                }
                frame_in = encoded_nv12;
                frame_bytes = (size_t)out_wstride * out_hstride * 3 / 2;
            } else {
                frame_in = rgb;
                frame_bytes = (size_t)out_w * out_h * 3;
            }
        } else if (a->fmt == FMT_YUV) {
            if (rot != 0) {
                if (rga_nv12_rot(nv12_frame, rotated_nv12, W, H, out_w, out_h, out_wstride, rot) != 0) {
                    cam_release(&cam, bi);
                    continue;
                }
                frame_in = rotated_nv12;
                frame_bytes = (size_t)out_wstride * out_hstride * 3 / 2;
            }
        }

        uint64_t pre_encode_us = now_us();
        uint32_t ts90k = (uint32_t)(((pre_encode_us - t0) * 90ull) / 1000ull);

        if (a->transport == TR_RTP) {
            const uint8_t *out; size_t olen; int key = 0;
            if (enc_encode(enc, frame_in, frame_bytes, &out, &olen, &key) == 0) {
                uint64_t post_encode_us = now_us();
                if (rtp_send_annexb(rtp, out, olen, ts90k) != 0)
                    fprintf(stderr, "[stream %d] RTP send failed\n", a->idx);
                uint64_t post_send_us = now_us();
                uint64_t process_us = pre_encode_us - pipeline_start_us;
                uint64_t encode_us = post_encode_us - pre_encode_us;
                uint64_t send_us = post_send_us - post_encode_us;
                capture_age_sum_us += capture_age_us;
                process_sum_us += process_us;
                encode_sum_us += encode_us;
                send_sum_us += send_us;
                if (capture_age_us > capture_age_max_us) capture_age_max_us = capture_age_us;
                if (process_us > process_max_us) process_max_us = process_us;
                if (encode_us > encode_max_us) encode_max_us = encode_us;
                if (send_us > send_max_us) send_max_us = send_us;
                latency_samples++;
            }
        } else {
            raw_send_frame(raw, frame_in, frame_bytes,
                           (uint16_t)W, (uint16_t)H, in_fmt_raw);
        }

        if (a->transport == TR_RTP)
            low_stream_offer(low, frame_in, rot ? out_wstride : out_w, out_h);
        cam_release(&cam, bi);

        frames++;
        uint64_t nt = now_us();
        if (nt - stat_t >= 5000000ull) {
            double fps = frames * 1e6 / (nt - stat_t);
            printf("[stream %d] %s 实时 %.1f fps\n", a->idx, dev, fps);
            if (latency_samples > 0) {
                printf("[latency %d] capture=%.2f/%.2fms process=%.2f/%.2fms "
                       "encode=%.2f/%.2fms send=%.2f/%.2fms (avg/max)\n",
                       a->idx,
                       capture_age_sum_us / (latency_samples * 1000.0),
                       capture_age_max_us / 1000.0,
                       process_sum_us / (latency_samples * 1000.0),
                       process_max_us / 1000.0,
                       encode_sum_us / (latency_samples * 1000.0),
                       encode_max_us / 1000.0,
                       send_sum_us / (latency_samples * 1000.0),
                       send_max_us / 1000.0);
                capture_age_sum_us = capture_age_max_us = 0;
                process_sum_us = process_max_us = 0;
                encode_sum_us = encode_max_us = 0;
                send_sum_us = send_max_us = 0;
                latency_samples = 0;
            }
            frames = 0; stat_t = nt;
        }
    }

cleanup:
    low_stream_destroy(low);
    if (rtp) rtp_close(rtp);
    if (raw) raw_close(raw);
    if (enc) enc_destroy(enc);
    if (rotated_nv12) free(rotated_nv12);
    if (encoded_nv12) free(encoded_nv12);
    if (rot != 0 && padded_rgb) free(padded_rgb);
    if (rgb) free(rgb);
    cam_close(&cam);
    printf("[stream %d] %s 已停止\n", a->idx, dev);
    return NULL;
}

static void usage(const char *prog)
{
    printf(
"用法: %s [全局选项] --cam DEV WxH PORT [--cam ...]\n"
"\n"
"全局选项:\n"
"  --host IP            对端板子 IP（默认 192.168.1.2）\n"
"  --format yuv|rgb     送编码/发送的像素格式（默认 rgb）\n"
"                       rgb=采集后经 RGA 转 RGB888 再编码（检测跟踪算法用这个）\n"
"                       yuv=相机原生 NV12 直接编码（最省、纯传输用）\n"
"  --codec  h264|h265   硬件编码格式（默认 h264）\n"
"  --rc     vbr|cbr      码率控制（默认 vbr=质量优先、静态省带宽）\n"
"  --transport rtp|raw  rtp=H264/H265 over RTP/UDP（默认，推荐）\n"
"                       raw=不压缩整帧分片 UDP（仅低码率/单路）\n"
"  --fps N              帧率（默认 30）\n"
"  --bitrate KBPS       每路目标码率（默认按分辨率自动: 720p≈6M,1080p≈10M）\n"
"  --gop N              I 帧间隔（默认 = 2*fps）\n"
"  -h, --help\n"
"\n"
"相机:\n"
"  --cam DEV WxH PORT [opts]   可重复，最多 %d 路。\n"
"      DEV  : 设备节点，如 /dev/video11\n"
"      WxH  : 分辨率，如 1920x1080（每路独立）\n"
"      PORT : 这一路的目标 UDP 端口（每路独立，要和接收端对应）\n"
"      opts : 可选，每路单独覆盖全局参数，逗号分隔 key=value：\n"
"             fps= bitrate= codec=h264|h265 format=rgb|yuv rc=vbr|cbr gop= host=\n"
"             input=nv12|yuyv|z16 depth_min=300 depth_max=5000\n"
"      例:\n"
"      --cam /dev/video11 1280x720 5600\n"
"      --cam /dev/video22 1920x1080 5601 codec=h265,fps=25,bitrate=8000\n"
"      --cam /dev/video0  1280x720 5602 host=192.168.1.9,format=yuv\n"
"\n"
"目标地址怎么对应:\n"
"  --host 是所有路共用的目标 IP（填下一跳：图传板 IP，或直连时填地面站 IP）；\n"
"  每路的 PORT 各不相同。接收端(中继/地面站)按 IP+PORT 一一对应收流。\n"
"  个别路要发到不同 IP 时，用该路 opts 里的 host= 单独覆盖。\n"
"\n"
"示例（两路相机 + 一路预留，H264 RTP 推到图传板 192.168.1.2）:\n"
"  %s --host 192.168.1.2 --format rgb --codec h264 \\\n"
"     --cam /dev/video11 1280x720 5600 \\\n"
"     --cam /dev/video22 1920x1080 5601\n",
        prog, MAX_CAMS, prog);
}

int main(int argc, char **argv)
{
    signal(SIGINT,  on_sig);
    signal(SIGTERM, on_sig);
    srand((unsigned)time(NULL));

    char    host[64]   = "192.168.1.2";
    PixMode fmt        = FMT_RGB;     /* 默认 RGB：采集后经 RGA 转 RGB888 再编码 */
    Transport transport= TR_RTP;
    EncCodec codec     = ENC_CODEC_H264;
    EncRcMode rc_mode  = ENC_RC_VBR;
    int     fps        = 30;
    int     bitrate    = 0;        /* 0 = 按分辨率自动 */
    int     gop        = 0;

    CamSpec cams[MAX_CAMS];
    int     ncam = 0;

    static struct option lo[] = {
        {"host",      required_argument, 0, 1},
        {"format",    required_argument, 0, 2},
        {"codec",     required_argument, 0, 3},
        {"transport", required_argument, 0, 4},
        {"fps",       required_argument, 0, 5},
        {"bitrate",   required_argument, 0, 6},
        {"gop",       required_argument, 0, 7},
        {"cam",       required_argument, 0, 8},
        {"rc",        required_argument, 0, 9},
        {"help",      no_argument,       0, 'h'},
        {0,0,0,0}
    };

    int c;
    while ((c = getopt_long(argc, argv, "h", lo, NULL)) != -1) {
        switch (c) {
        case 1: snprintf(host, sizeof(host), "%s", optarg); break;
        case 2: fmt = strcmp(optarg, "rgb") == 0 ? FMT_RGB : FMT_YUV; break;
        case 3: codec = strcmp(optarg, "h265") == 0 ? ENC_CODEC_H265 : ENC_CODEC_H264; break;
        case 4: transport = strcmp(optarg, "raw") == 0 ? TR_RAW : TR_RTP; break;
        case 5: fps = atoi(optarg); break;
        case 6: bitrate = atoi(optarg); break;
        case 7: gop = atoi(optarg); break;
        case 9: rc_mode = strcmp(optarg, "cbr") == 0 ? ENC_RC_CBR : ENC_RC_VBR; break;
        case 8: {
            /* --cam DEV WxH PORT [opts] : optarg=DEV，随后 WxH、PORT，可选 opts(含=) */
            if (ncam >= MAX_CAMS) { fprintf(stderr, "相机数超过 %d\n", MAX_CAMS); return 1; }
            if (optind + 1 >= argc) { fprintf(stderr, "--cam 需要 DEV WxH PORT\n"); return 1; }
            CamSpec *cs = &cams[ncam];
            memset(cs, 0, sizeof(*cs));
            cs->fps = cs->bitrate = cs->codec = cs->fmt = cs->rc = cs->gop = USE_GLOBAL;
            cs->input = INPUT_NV12;
            cs->depth_min_mm = 300;
            cs->depth_max_mm = 5000;
            cs->rot = 0;
            cs->host[0] = 0;
            snprintf(cs->dev, sizeof(cs->dev), "%s", optarg);
            const char *res = argv[optind++];
            const char *prt = argv[optind++];
            if (sscanf(res, "%dx%d", &cs->width, &cs->height) != 2) {
                fprintf(stderr, "分辨率格式应为 WxH，例 1920x1080\n"); return 1;
            }
            cs->port = atoi(prt);
            /* 可选的每路参数串：下一个 token 不是选项且含 '=' */
            if (optind < argc && argv[optind][0] != '-' && strchr(argv[optind], '=')) {
                if (parse_cam_opts(cs, argv[optind++]) != 0) return 1;
            }
            ncam++;
            break;
        }
        case 'h': default: usage(argv[0]); return (c == 'h') ? 0 : 1;
        }
    }
    if (ncam == 0) {
        fprintf(stderr, "错误: 至少指定一路 --cam\n\n");
        usage(argv[0]);
        return 1;
    }

    printf("===== RK3588 多路相机推流 =====\n");
    printf("对端: %s  格式: %s  编码: %s  码控: %s  传输: %s  fps:%d  码率:%s\n",
           host, fmt == FMT_RGB ? "RGB" : "YUV",
           codec == ENC_CODEC_H265 ? "H265" : "H264",
           rc_mode == ENC_RC_CBR ? "CBR" : "VBR",
           transport == TR_RAW ? "RAW" : "RTP", fps,
           bitrate > 0 ? "手动" : "按分辨率自动");

    pthread_t  th[MAX_CAMS];
    ThreadArg  ta[MAX_CAMS];
    for (int i = 0; i < ncam; i++) {
        ta[i].spec = cams[i];
        /* 每路参数：有覆盖用覆盖，否则沿用全局 */
        snprintf(ta[i].host, sizeof(ta[i].host), "%s",
                 cams[i].host[0] ? cams[i].host : host);
        ta[i].fmt       = (cams[i].fmt   == USE_GLOBAL) ? fmt     : (PixMode)cams[i].fmt;
        ta[i].codec     = (cams[i].codec == USE_GLOBAL) ? codec   : (EncCodec)cams[i].codec;
        ta[i].rc_mode   = (cams[i].rc    == USE_GLOBAL) ? rc_mode : (EncRcMode)cams[i].rc;
        ta[i].fps       = (cams[i].fps   == USE_GLOBAL) ? fps     : cams[i].fps;
        ta[i].bitrate   = (cams[i].bitrate == USE_GLOBAL) ? bitrate : cams[i].bitrate;
        ta[i].gop       = (cams[i].gop   == USE_GLOBAL) ? gop     : cams[i].gop;
        ta[i].input     = (InputFormat)cams[i].input;
        ta[i].depth_min_mm = cams[i].depth_min_mm;
        ta[i].depth_max_mm = cams[i].depth_max_mm;
        ta[i].rot = cams[i].rot;
        ta[i].transport = transport;
        ta[i].idx = i;
        pthread_create(&th[i], NULL, cam_thread, &ta[i]);
    }

    while (g_running) usleep(100000);

    for (int i = 0; i < ncam; i++) pthread_join(th[i], NULL);
    printf("全部停止，退出。\n");
    return g_camera_fault ? 2 : 0;
}
