/**
 * rtsp_relay.c — 把 rk_streamer 推来的本地 RTP 流，挂成标准 RTSP 地址
 * (回退到稳定旧版本,只保留 shared=TRUE,去掉所有引入不稳定的修改)
 */
#include <gst/gst.h>
#include <gst/rtsp-server/rtsp-server.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

#define MAX_MOUNTS 16

typedef struct {
    char path[64];
    int  port;
    int  is_h265;
    int control_port;
    struct sockaddr_in control_addr;
} Mount;

typedef struct {
    gint64 pending[MAX_MOUNTS];
    gboolean playing[MAX_MOUNTS];
    gint64 last_request;
} DemandClient;
typedef struct {
    Mount *mounts;
    int count, fd;
    GMutex mutex;
    GHashTable *clients;
    int counts[MAX_MOUNTS];
} Demand;

static int mount_index(Demand *d, GstRTSPContext *ctx) {
    if(!ctx || !ctx->uri || !ctx->uri->abspath)return -1;
    const char *path=ctx->uri->abspath;
    for(int i=0;i<d->count;++i) {
        size_t n=strlen(d->mounts[i].path);
        if(d->mounts[i].control_port && !strncmp(path,d->mounts[i].path,n) &&
           (!path[n] || path[n]=='/'))return i;
    }
    return -1;
}
static void lease_send(Demand *d,int index,gboolean key) {
    Mount *m=&d->mounts[index];
    const char *msg=key?"UAV_LOW_KEY_V1":"UAV_LOW_V1";
    if(d->fd>=0)sendto(d->fd,msg,strlen(msg),MSG_DONTWAIT,
        (struct sockaddr*)&m->control_addr,sizeof(m->control_addr));
}
/* Trigger before DESCRIBE: media preparation needs the first SPS/PPS.
 * https://gstreamer.freedesktop.org/documentation/gst-rtsp-server/rtsp-client.html */
static GstRTSPStatusCode demand_prepare(GstRTSPClient *client,GstRTSPContext *ctx,gpointer data) {
    Demand *d=data;int index=mount_index(d,ctx);
    if(index<0)return GST_RTSP_STS_OK;
    g_mutex_lock(&d->mutex);
    DemandClient *s=g_hash_table_lookup(d->clients,client);
    if(s) {s->pending[index]=g_get_monotonic_time()+8000000;s->last_request=g_get_monotonic_time();}
    lease_send(d,index,TRUE);
    g_mutex_unlock(&d->mutex);
    return GST_RTSP_STS_OK;
}
static void demand_play(GstRTSPClient *client,GstRTSPContext *ctx,gpointer data) {
    Demand *d=data;int i=mount_index(d,ctx);if(i<0)return;
    g_mutex_lock(&d->mutex);DemandClient *s=g_hash_table_lookup(d->clients,client);
    if(s){s->playing[i]=TRUE;s->pending[i]=0;s->last_request=g_get_monotonic_time();}
    lease_send(d,i,TRUE);g_mutex_unlock(&d->mutex);
}
static void demand_stop(GstRTSPClient *client,GstRTSPContext *ctx,gpointer data) {
    Demand *d=data;int i=mount_index(d,ctx);if(i<0)return;
    g_mutex_lock(&d->mutex);DemandClient *s=g_hash_table_lookup(d->clients,client);
    if(s){s->playing[i]=FALSE;s->pending[i]=0;}
    g_mutex_unlock(&d->mutex);
}
static void demand_touch(GstRTSPClient *client,GstRTSPContext *ctx,gpointer data) {
    (void)ctx;Demand *d=data;g_mutex_lock(&d->mutex);
    DemandClient *s=g_hash_table_lookup(d->clients,client);
    if(s)s->last_request=g_get_monotonic_time();
    g_mutex_unlock(&d->mutex);
}
static void demand_closed(GstRTSPClient *client,gpointer data) {
    Demand *d=data;g_mutex_lock(&d->mutex);
    g_hash_table_remove(d->clients,client);g_mutex_unlock(&d->mutex);
}
static void demand_connected(GstRTSPServer *server,GstRTSPClient *client,gpointer data) {
    (void)server;Demand *d=data;DemandClient *s=g_new0(DemandClient,1);
    s->last_request=g_get_monotonic_time();g_mutex_lock(&d->mutex);
    g_hash_table_insert(d->clients,client,s);g_mutex_unlock(&d->mutex);
    g_signal_connect(client,"pre-describe-request",G_CALLBACK(demand_prepare),d);
    g_signal_connect(client,"pre-setup-request",G_CALLBACK(demand_prepare),d);
    g_signal_connect(client,"play-request",G_CALLBACK(demand_play),d);
    g_signal_connect(client,"pause-request",G_CALLBACK(demand_stop),d);
    g_signal_connect(client,"teardown-request",G_CALLBACK(demand_stop),d);
    g_signal_connect(client,"get-parameter-request",G_CALLBACK(demand_touch),d);
    g_signal_connect(client,"options-request",G_CALLBACK(demand_touch),d);
    g_signal_connect(client,"closed",G_CALLBACK(demand_closed),d);
}
static gboolean demand_tick(gpointer data) {
    Demand *d=data;int counts[MAX_MOUNTS]={0};gint64 now=g_get_monotonic_time();
    g_mutex_lock(&d->mutex);GHashTableIter iter;gpointer value;
    g_hash_table_iter_init(&iter,d->clients);
    while(g_hash_table_iter_next(&iter,NULL,&value)) {
        DemandClient *s=value;
        for(int i=0;i<d->count;++i)
            if((s->playing[i] && now-s->last_request<65000000) || s->pending[i]>now)++counts[i];
    }
    for(int i=0;i<d->count;++i)if(d->mounts[i].control_port) {
        if(counts[i])lease_send(d,i,FALSE);
        if(counts[i]!=d->counts[i])g_print("[demand] %s clients=%d\n",d->mounts[i].path,counts[i]);
        d->counts[i]=counts[i];
    }
    g_mutex_unlock(&d->mutex);return G_SOURCE_CONTINUE;
}

int main(int argc, char **argv)
{
    gst_init(&argc, &argv);

    int service_port = 8554;
    Mount mounts[MAX_MOUNTS] = {0};
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
        } else if (!strcmp(argv[i], "--demand") && i + 3 < argc) {
            const char *path=argv[++i],*host=argv[++i];int port=atoi(argv[++i]);
            int found=0;
            for(int j=0;j<nm;++j)if(!strcmp(path,mounts[j].path)) {
                Mount *m=&mounts[j];m->control_port=port;
                m->control_addr.sin_family=AF_INET;m->control_addr.sin_port=htons(port);
                if(port<1024 || port>65535 || inet_pton(AF_INET,host,&m->control_addr.sin_addr)!=1)return 1;
                found=1;break;
            }
            if(!found){fprintf(stderr,"--demand must follow its --mount\n");return 1;}
        } else if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
            printf("用法: %s [--port 8554] --mount /路径 RTP端口 h264|h265 [--demand /路径 控制IP 控制UDP端口]\n", argv[0]);
            return 0;
        }
    }
    if (nm == 0) {
        fprintf(stderr, "错误: 至少一个 --mount /路径 端口 h264|h265\n");
        return 1;
    }

    GMainLoop *loop = g_main_loop_new(NULL, FALSE);
    GstRTSPServer *server = gst_rtsp_server_new();
    Demand demand={.mounts=mounts,.count=nm,.fd=socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0)};
    g_mutex_init(&demand.mutex);
    demand.clients=g_hash_table_new_full(g_direct_hash,g_direct_equal,NULL,g_free);
    g_signal_connect(server,"client-connected",G_CALLBACK(demand_connected),&demand);
    g_timeout_add(1000,demand_tick,&demand);
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
