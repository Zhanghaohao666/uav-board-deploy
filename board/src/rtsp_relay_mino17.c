/**
 * rtsp_relay.c — 把 rk_streamer 推来的本地 RTP 流，挂成标准 RTSP 地址
 * (回退到稳定旧版本,只保留 shared=TRUE,去掉所有引入不稳定的修改)
 */
#include <gst/gst.h>
#include <gst/rtsp-server/rtsp-server.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#define MAX_MOUNTS 8

typedef struct {
    char path[64];
    int  port;
    int  is_h265;
} Mount;

int main(int argc, char **argv)
{
    gst_init(&argc, &argv);

    int service_port = 8554;
    Mount mounts[MAX_MOUNTS];
    int nm = 0;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--port") && i + 1 < argc) {
            service_port = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--mount") && i + 3 < argc) {
            if (nm >= MAX_MOUNTS) { fprintf(stderr, "挂载点过多\n"); return 1; }
            Mount *m = &mounts[nm];
            snprintf(m->path, sizeof(m->path), "%s", argv[++i]);
            m->port    = atoi(argv[++i]);
            m->is_h265 = (strcmp(argv[++i], "h265") == 0);
            nm++;
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            printf("用法: %s [--port 8554] --mount /路径 监听端口 h264|h265 [--mount ...]\n", argv[0]);
            return 0;
        }
    }
    if (nm == 0) {
        fprintf(stderr, "错误: 至少一个 --mount /路径 端口 h264|h265\n");
        return 1;
    }

    GMainLoop *loop = g_main_loop_new(NULL, FALSE);
    GstRTSPServer *server = gst_rtsp_server_new();
    /* A missing RTP source may block DESCRIBE while media is prepared.
     * Give each client its own request thread so other mounts remain usable. */
    GstRTSPThreadPool *pool = gst_rtsp_server_get_thread_pool(server);
    gst_rtsp_thread_pool_set_max_threads(pool, -1);
    g_object_unref(pool);
    char svc[16];
    snprintf(svc, sizeof(svc), "%d", service_port);
    gst_rtsp_server_set_service(server, svc);

    GstRTSPMountPoints *mountpts = gst_rtsp_server_get_mount_points(server);

    for (int i = 0; i < nm; i++) {
        Mount *m = &mounts[i];
        char launch[512];
        const char *enc = m->is_h265 ? "H265" : "H264";
        const char *depay = m->is_h265 ? "rtph265depay" : "rtph264depay";
        const char *pay   = m->is_h265 ? "rtph265pay"   : "rtph264pay";
        const char *jitter = !strcmp(m->path, "/infrared")
            ? "rtpjitterbuffer latency=50 drop-on-latency=true ! " : "";
        snprintf(launch, sizeof(launch),
            "( udpsrc port=%d caps=\"application/x-rtp,media=video,"
            "clock-rate=90000,encoding-name=%s,payload=96\" ! "
            "%s%s ! %s name=pay0 pt=96 )",
            m->port, enc, jitter, depay, pay);

        GstRTSPMediaFactory *factory = gst_rtsp_media_factory_new();
        gst_rtsp_media_factory_set_launch(factory, launch);
        gst_rtsp_media_factory_set_shared(factory, TRUE);   /* 多客户端共享一路 */
        gst_rtsp_mount_points_add_factory(mountpts, m->path, factory);

        g_print("[rtsp] 挂载 rtsp://<本机IP>:%d%s  <-  udp:%d (%s)\n",
                service_port, m->path, m->port, enc);
    }
    g_object_unref(mountpts);

    if (gst_rtsp_server_attach(server, NULL) == 0) {
        g_printerr("RTSP 服务启动失败（端口 %d 被占用？）\n", service_port);
        return 1;
    }
    g_print("[rtsp] RTSP 服务已启动，端口 %d (shared=TRUE,infrared RTP clock recovery)。Ctrl+C 退出。\n", service_port);
    g_main_loop_run(loop);
    return 0;
}
