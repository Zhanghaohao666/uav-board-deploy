#define _POSIX_C_SOURCE 200809L
#include "camera.h"
#include "mpp_encoder.h"
#include "rtp_sender.h"
#include "mino_unpack.h"
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static volatile sig_atomic_t running = 1;
static void stop(int sig) { (void)sig; running = 0; }
static uint64_t mono_us(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000 + t.tv_nsec / 1000;
}
static int number(const char *s, int min, int max) {
    char *end;
    long n = strtol(s, &end, 10);
    return *s && !*end && n >= min && n <= max ? (int)n : -1;
}
int main(int argc, char **argv) {
    const char *device = "/dev/v4l/by-id/usb-CHIPUP_Mino17_Mino17-video-index0";
    const char *host = "192.168.10.1";
    int port = 5604, bitrate = 1500, rotate = 0;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--help")) {
            puts("mino17_streamer [--device PATH] [--host IP] [--port N] [--bitrate KBPS] [--rotate 0|180]");
            return 0;
        }
        if (i + 1 >= argc) { fprintf(stderr, "Missing argument: %s\n", argv[i]); return 2; }
        const char *key = argv[i++], *value = argv[i];
        if (!strcmp(key, "--device")) device = value;
        else if (!strcmp(key, "--host")) host = value;
        else if (!strcmp(key, "--port")) port = number(value, 1, 65535);
        else if (!strcmp(key, "--bitrate")) bitrate = number(value, 100, 20000);
        else if (!strcmp(key, "--rotate")) rotate = number(value, 0, 180);
        else { fprintf(stderr, "Unknown option: %s\n", key); return 2; }
    }
    if (port < 0 || bitrate < 0 || (rotate != 0 && rotate != 180)) return 2;
    setvbuf(stdout, NULL, _IOLBF, 0);
    struct sigaction sa = {0};
    sa.sa_handler = stop;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    Camera cam;
    /* Explicitly request the documented UYVY transport format. */
    if (cam_open(&cam, device, 1280, 520, V4L2_PIX_FMT_UYVY, 4, 25)) return 1;
    struct v4l2_format fmt = {0};
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (cam.width != 1280 || cam.height != 520 || cam.use_mplane ||
        ioctl(cam.fd, VIDIOC_G_FMT, &fmt) < 0 || fmt.fmt.pix.bytesperline < 2560) {
        fprintf(stderr, "Expected Mino17 1280x520 single-plane RAW+UYVY layout\n");
        cam_close(&cam);
        return 1;
    }
    size_t stride = fmt.fmt.pix.bytesperline;
    EncConfig cfg = {.width=640, .height=512, .in_fmt=ENC_FMT_NV12,
        .codec=ENC_CODEC_H264, .rc_mode=ENC_RC_CBR, .fps=25,
        .bitrate_kbps=bitrate, .gop=25};
    MppEncoder *enc = enc_create(&cfg);
    RtpSender *rtp = rtp_open(host, port, 0);
    const size_t size = 640 * 512 * 3 / 2;
    uint8_t *nv12 = malloc(size);
    int failed = !enc || !rtp || !nv12;
    uint64_t report = mono_us(), frames = 0;
    uint32_t rtp_timestamp = 0;
    int capture_errors = 0;
    printf("[infrared] %s RAW+UYVY 1280x520 -> 640x512 H264 25fps %dkbps rotate=%d -> %s:%d\n",
           device, bitrate, rotate, host, port);
    while (running && !failed) {
        void *data;
        size_t len;
        int index;
        int rc = cam_grab(&cam, &data, &len, &index);
        if (rc != 0) {
            if (!running) break;
            if (rc == -2 || ++capture_errors >= 3) failed = 1;
            continue;
        }
        capture_errors = 0;
        rc = mino_unpack_nv12(data, len, stride, nv12, size, rotate == 180);
        int released = cam_release(&cam, index);
        if (rc || released) { fprintf(stderr, "[infrared] invalid frame or device disconnected\n"); failed=1; break; }
        const uint8_t *packet;
        size_t plen;
        int keyframe;
        if (enc_encode(enc, nv12, size, &packet, &plen, &keyframe) ||
            rtp_send_annexb(rtp, packet, plen, rtp_timestamp)) {
            fprintf(stderr, "[infrared] encode/send failure\n"); failed=1; break;
        }
        /* Fixed 90 kHz / 25 fps clock excludes USB arrival jitter. */
        rtp_timestamp += 3600;
        ++frames;
        uint64_t now = mono_us();
        if (now-report >= 10000000) {
            printf("[infrared] %.2f fps, %llu frames in sample\n",
                   frames*1000000.0/(now-report), (unsigned long long)frames);
            report=now; frames=0;
        }
    }
    free(nv12);
    if (rtp) rtp_close(rtp);
    if (enc) enc_destroy(enc);
    cam_close(&cam);
    return failed ? 1 : 0;
}
