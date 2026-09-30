/**
 * rtp_sender.c — RTP/UDP 发送器实现
 *  - H264: RFC 6184（单 NAL 包 + FU-A 分片）
 *  - H265: RFC 7798（单 NAL 包 + FU 分片）
 *  - raw : 自定义分片协议（不压缩）
 */
#include "rtp_sender.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <netinet/in.h>

#define RTP_MTU       1400          /* RTP 负载上限，避免 IP 分片 */
#define RTP_VERSION   2
#define PT_DYNAMIC    96

struct RtpSender {
    int                 fd;
    struct sockaddr_in  dst;
    uint16_t            seq;
    uint32_t            ssrc;
    int                 is_h265;
};

static int udp_socket(const char *ip, int port, struct sockaddr_in *out)
{
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("[net] socket"); return -1; }
    /* 增大发送缓冲，降低突发丢包 */
    int snd = 1 << 21;
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &snd, sizeof(snd));
    memset(out, 0, sizeof(*out));
    out->sin_family = AF_INET;
    out->sin_port   = htons(port);
    if (inet_pton(AF_INET, ip, &out->sin_addr) != 1) {
        fprintf(stderr, "[net] bad ip %s\n", ip);
        close(fd);
        return -1;
    }
    return fd;
}

RtpSender *rtp_open(const char *dst_ip, int dst_port, int payload_h265)
{
    RtpSender *s = (RtpSender *)calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->fd = udp_socket(dst_ip, dst_port, &s->dst);
    if (s->fd < 0) { free(s); return NULL; }
    s->seq     = (uint16_t)(rand() & 0xFFFF);
    s->ssrc    = (uint32_t)rand();
    s->is_h265 = payload_h265;
    printf("[rtp] -> %s:%d (%s)\n", dst_ip, dst_port,
           payload_h265 ? "H265" : "H264");
    return s;
}

/* 写 12 字节 RTP 头 */
static void put_rtp_hdr(RtpSender *s, uint8_t *p, int marker, uint32_t ts)
{
    p[0] = (RTP_VERSION << 6);
    p[1] = (uint8_t)((marker ? 0x80 : 0) | PT_DYNAMIC);
    p[2] = s->seq >> 8;  p[3] = s->seq & 0xFF;
    p[4] = ts >> 24; p[5] = ts >> 16; p[6] = ts >> 8; p[7] = ts;
    p[8] = s->ssrc >> 24; p[9] = s->ssrc >> 16; p[10] = s->ssrc >> 8; p[11] = s->ssrc;
    s->seq++;
}

static int send_pkt(RtpSender *s, const uint8_t *buf, int len)
{
    return sendto(s->fd, buf, len, 0,
                  (struct sockaddr *)&s->dst, sizeof(s->dst));
}

/* 发送单个 NAL（不含起始码），按需 FU 分片。marker=该帧最后一个 NAL */
static int send_nal(RtpSender *s, const uint8_t *nal, int nal_len,
                    uint32_t ts, int marker)
{
    uint8_t pkt[12 + RTP_MTU + 4];
    const int min_header = s && s->is_h265 ? 2 : 1;
    if (!s || !nal || nal_len < min_header) return -1;

    if (nal_len <= RTP_MTU) {
        /* 单 NAL 单包 */
        put_rtp_hdr(s, pkt, marker, ts);
        memcpy(pkt + 12, nal, nal_len);
        return send_pkt(s, pkt, 12 + nal_len) == 12 + nal_len ? 0 : -1;
    }

    if (!s->is_h265) {
        /* H264 FU-A: FU indicator(1) + FU header(1) */
        uint8_t nri  = nal[0] & 0x60;
        uint8_t type = nal[0] & 0x1F;
        const uint8_t *data = nal + 1;
        int remain = nal_len - 1;
        int first = 1;
        while (remain > 0) {
            int payload = remain > (RTP_MTU - 2) ? (RTP_MTU - 2) : remain;
            int last = (payload == remain);
            put_rtp_hdr(s, pkt, (last && marker) ? 1 : 0, ts);
            pkt[12] = nri | 28;                 /* FU-A type=28 */
            pkt[13] = (first ? 0x80 : 0) | (last ? 0x40 : 0) | type;
            memcpy(pkt + 14, data, payload);
            if (send_pkt(s, pkt, 14 + payload) != 14 + payload) return -1;
            data   += payload;
            remain -= payload;
            first   = 0;
        }
    } else {
        /* H265 FU: 2 字节 NAL 头 + 1 字节 FU 头 */
        uint8_t type = (nal[0] >> 1) & 0x3F;
        uint8_t layer_tid_hi = nal[0] & 0x81;   /* F(1)+保留 高位 */
        uint8_t tid = nal[1];
        const uint8_t *data = nal + 2;
        int remain = nal_len - 2;
        int first = 1;
        while (remain > 0) {
            int payload = remain > (RTP_MTU - 3) ? (RTP_MTU - 3) : remain;
            int last = (payload == remain);
            put_rtp_hdr(s, pkt, (last && marker) ? 1 : 0, ts);
            pkt[12] = layer_tid_hi | (49 << 1); /* FU type=49 */
            pkt[13] = tid;
            pkt[14] = (first ? 0x80 : 0) | (last ? 0x40 : 0) | type;
            memcpy(pkt + 15, data, payload);
            if (send_pkt(s, pkt, 15 + payload) != 15 + payload) return -1;
            data   += payload;
            remain -= payload;
            first   = 0;
        }
    }
    return 0;
}

int rtp_send_annexb(RtpSender *s, const uint8_t *d, size_t len, uint32_t ts90k)
{
    if (!s || !d || len < 4) return -1;
    /* 切分 Annex-B 起始码，逐个 NAL 发送 */
    size_t i = 0, n = len;
    /* 收集每个 NAL 的 [start,end)，最后一个置 marker */
    size_t nal_start = 0;
    int have = 0;
    /* 先定位所有起始码位置 */
    /* 为了置 marker，我们两遍：先记录上一个 NAL，遇到下一个再发上一个 */
    size_t prev_off = 0, prev_len = 0;
    have = 0;

    i = 0;
    while (i + 3 < n) {
        int sc = 0, sclen = 0;
        if (d[i] == 0 && d[i+1] == 0 && d[i+2] == 1)            { sc = 1; sclen = 3; }
        else if (i + 4 < n && d[i]==0 && d[i+1]==0 && d[i+2]==0 && d[i+3]==1) { sc = 1; sclen = 4; }
        if (sc) {
            nal_start = i + sclen;
            if (have) {
                prev_len = i - prev_off;
                if (send_nal(s, d + prev_off, (int)prev_len, ts90k, 0) != 0)
                    return -1;
            }
            prev_off = nal_start;
            have = 1;
            i = nal_start;
        } else {
            i++;
        }
    }
    if (have) {
        prev_len = n - prev_off;
        if (send_nal(s, d + prev_off, (int)prev_len, ts90k, 1) != 0)
            return -1; /* 最后一个置 marker */
    }
    return have ? 0 : -1;
}

void rtp_close(RtpSender *s)
{
    if (!s) return;
    if (s->fd >= 0) close(s->fd);
    free(s);
}

/* ===================== raw 不压缩传输 ===================== */
#define RAW_MAGIC 0x52565258u   /* "RVRX" */
#define RAW_MTU   1400

struct RawSender {
    int                 fd;
    struct sockaddr_in  dst;
    uint32_t            seq;
};

#pragma pack(push, 1)
typedef struct {
    uint32_t magic;
    uint32_t seq;        /* 帧序号 */
    uint32_t total_len;  /* 整帧字节数 */
    uint32_t offset;     /* 本分片在帧中的偏移 */
    uint16_t w;
    uint16_t h;
    uint8_t  fmt;        /* 0=NV12 1=RGB888 */
    uint8_t  rsv[3];
} RawHdr;
#pragma pack(pop)

RawSender *raw_open(const char *dst_ip, int dst_port)
{
    RawSender *s = (RawSender *)calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->fd = udp_socket(dst_ip, dst_port, &s->dst);
    if (s->fd < 0) { free(s); return NULL; }
    printf("[raw] -> %s:%d\n", dst_ip, dst_port);
    return s;
}

int raw_send_frame(RawSender *s, const void *data, size_t len,
                   uint16_t w, uint16_t h, uint8_t fmt)
{
    const uint8_t *p = (const uint8_t *)data;
    uint8_t pkt[sizeof(RawHdr) + RAW_MTU];
    uint32_t off = 0;
    s->seq++;
    while (off < len) {
        uint32_t chunk = (len - off > RAW_MTU) ? RAW_MTU : (uint32_t)(len - off);
        RawHdr *hdr = (RawHdr *)pkt;
        hdr->magic = RAW_MAGIC;
        hdr->seq   = s->seq;
        hdr->total_len = (uint32_t)len;
        hdr->offset = off;
        hdr->w = w; hdr->h = h; hdr->fmt = fmt;
        hdr->rsv[0] = hdr->rsv[1] = hdr->rsv[2] = 0;
        memcpy(pkt + sizeof(RawHdr), p + off, chunk);
        sendto(s->fd, pkt, sizeof(RawHdr) + chunk, 0,
               (struct sockaddr *)&s->dst, sizeof(s->dst));
        off += chunk;
    }
    return 0;
}

void raw_close(RawSender *s)
{
    if (!s) return;
    if (s->fd >= 0) close(s->fd);
    free(s);
}
