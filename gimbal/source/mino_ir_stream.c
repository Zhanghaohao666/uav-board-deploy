#include "mino_ir_stream.h"
#include "low_stream.h"
#include "mino_unpack.h"
#include "stream_tuning.h"
#include "rk_mpi_sys.h"
#include "rk_mpi_mb.h"
#include "rk_mpi_venc.h"
#include <errno.h>
#include <limits.h>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <time.h>
#include <unistd.h>

#define IR_CHN 1
#define IR_W 640
#define IR_H 512
#define IR_BYTES (IR_W * IR_H * 3 / 2)
#define UVC_BUFFERS 4
struct capture {
    int fd, streaming;
    unsigned count, stride;
    int hik, fps;
    void *data[UVC_BUFFERS];
    size_t length[UVC_BUFFERS];
};
static pthread_t worker;
static int worker_started, stopping;
static rtsp_demo_handle server;
static rtsp_session_handle session;
static pthread_mutex_t *server_mutex;
static char device[128];
static int rotate180, bitrate, gop;
static uint64_t now_us(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}
static int stopped(void) { return __atomic_load_n(&stopping, __ATOMIC_ACQUIRE); }
static int xioctl(int fd, unsigned long request, void *arg) {
    int rc; do { rc = ioctl(fd, request, arg); } while (rc < 0 && errno == EINTR);
    return rc;
}
static void capture_close(struct capture *c) {
    if (c->fd < 0) return;
    if (c->streaming) {
        enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        xioctl(c->fd, VIDIOC_STREAMOFF, &type);
    }
    for (unsigned i = 0; i < c->count; ++i)
        if (c->data[i] && c->data[i] != MAP_FAILED) munmap(c->data[i], c->length[i]);
    close(c->fd); c->fd = -1;
}
/* Match the actual thermal USB identity, never an arbitrary visible UVC camera. */
static int hik_identity(const char *path) {
    const char *base = strrchr(path, '/');
    char sys[256], real[PATH_MAX], file[PATH_MAX+32], vendor[16], product[16];
    snprintf(sys, sizeof(sys), "/sys/class/video4linux/%s/device", base ? base + 1 : path);
    if (!realpath(sys, real)) return 0;
    for (int depth=0; depth<6; depth++) {
        snprintf(file,sizeof(file),"%s/idVendor",real);
        FILE *f=fopen(file,"r");
        if (f) {
            int a=fscanf(f,"%15s",vendor); fclose(f);
            snprintf(file,sizeof(file),"%s/idProduct",real); f=fopen(file,"r");
            if (!f) return 0;
            int b=fscanf(f,"%15s",product); fclose(f);
            return a==1 && b==1 && !strcmp(vendor,"2bdf") && !strcmp(product,"0102");
        }
        char *slash=strrchr(real,'/'); if (!slash || slash==real) break; *slash=0;
    }
    return 0;
}
static int open_mino(const char *path, int *hik) {
    int fd = open(path, O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) return -1;
    struct v4l2_capability cap = {0};
    if (xioctl(fd, VIDIOC_QUERYCAP, &cap) < 0) {close(fd);return -1;}
    unsigned caps = (cap.capabilities & V4L2_CAP_DEVICE_CAPS) ? cap.device_caps : cap.capabilities;
    *hik = hik_identity(path);
    if ((!strstr((char *)cap.card, "Mino17") && !*hik) || !(caps & V4L2_CAP_VIDEO_CAPTURE) ||
        !(caps & V4L2_CAP_STREAMING)) {close(fd);errno=ENODEV;return -1;}
    return fd;
}
static int capture_open(struct capture *c) {
    memset(c, 0, sizeof(*c)); c->fd = -1;
    char selected[128];
    if (device[0]) {
        snprintf(selected, sizeof(selected), "%s", device);
        c->fd = open_mino(selected, &c->hik);
    } else {
        for (int i = 0; i < 256 && c->fd < 0; ++i) {
            snprintf(selected, sizeof(selected), "/dev/video%d", i);
            c->fd = open_mino(selected, &c->hik);
        }
    }
    if (c->fd < 0) {fprintf(stderr,"[IR] supported thermal capture device unavailable\n");return -1;}
    struct v4l2_format fmt = {0};
    fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    c->fps = c->hik ? 30 : 25;
    unsigned want_w=c->hik ? 640 : 1280, want_h=c->hik ? 1032 : 520;
    int current=xioctl(c->fd,VIDIOC_G_FMT,&fmt);
    if (current<0 || fmt.fmt.pix.width!=want_w || fmt.fmt.pix.height!=want_h ||
        fmt.fmt.pix.pixelformat!=V4L2_PIX_FMT_YUYV) {
        memset(&fmt,0,sizeof(fmt));fmt.type=V4L2_BUF_TYPE_VIDEO_CAPTURE;
        fmt.fmt.pix.width=want_w;fmt.fmt.pix.height=want_h;
        fmt.fmt.pix.pixelformat=V4L2_PIX_FMT_YUYV;fmt.fmt.pix.field=V4L2_FIELD_NONE;
        if (xioctl(c->fd,VIDIOC_S_FMT,&fmt)<0) {
            /* Some HIK UVC firmware rejects S_FMT while retaining its valid
             * default layout. Verify the effective format instead of assuming. */
            memset(&fmt,0,sizeof(fmt));fmt.type=V4L2_BUF_TYPE_VIDEO_CAPTURE;
            if (xioctl(c->fd,VIDIOC_G_FMT,&fmt)<0) goto fail;
        }
    }
    if (fmt.fmt.pix.width!=want_w || fmt.fmt.pix.height!=want_h ||
        fmt.fmt.pix.pixelformat!=V4L2_PIX_FMT_YUYV || fmt.fmt.pix.bytesperline<want_w*2) {
        fprintf(stderr,"[IR] unsupported thermal wire format: %ux%u fourcc=%08x stride=%u\n",
                fmt.fmt.pix.width,fmt.fmt.pix.height,fmt.fmt.pix.pixelformat,fmt.fmt.pix.bytesperline);
        goto fail;
    }
    c->stride=fmt.fmt.pix.bytesperline;
    struct v4l2_streamparm parm={0};parm.type=fmt.type;
    parm.parm.capture.timeperframe.numerator=1;
    parm.parm.capture.timeperframe.denominator=c->fps;
    xioctl(c->fd,VIDIOC_S_PARM,&parm);
    struct v4l2_requestbuffers req = {0}; req.count = UVC_BUFFERS;
    req.type = fmt.type; req.memory = V4L2_MEMORY_MMAP;
    if (xioctl(c->fd, VIDIOC_REQBUFS, &req) < 0 || req.count < 2) goto fail;
    c->count = req.count < UVC_BUFFERS ? req.count : UVC_BUFFERS;
    for (unsigned i = 0; i < c->count; ++i) {
        struct v4l2_buffer b = {0}; b.type = fmt.type; b.memory = V4L2_MEMORY_MMAP; b.index = i;
        if (xioctl(c->fd, VIDIOC_QUERYBUF, &b) < 0) goto fail;
        c->length[i] = b.length;
        c->data[i] = mmap(NULL, b.length, PROT_READ | PROT_WRITE, MAP_SHARED, c->fd, b.m.offset);
        if (c->data[i] == MAP_FAILED || xioctl(c->fd, VIDIOC_QBUF, &b) < 0) goto fail;
    }
    enum v4l2_buf_type type = fmt.type;
    if (xioctl(c->fd, VIDIOC_STREAMON, &type) < 0) goto fail;
    c->streaming = 1;
    fprintf(stderr,"[IR] capture %s: %s stride=%u -> 640x512 NV12 @%dfps\n",selected,c->hik?"HIK 640x1032, YUYV rows 516..1027":"Mino17 1280x520, right UYVY",c->stride,c->fps);
    return 0;
fail:
    fprintf(stderr,"[IR] UVC setup failed: %s\n",strerror(errno));capture_close(c);return -1;
}
static int encoder_open(int fps) {
    VENC_CHN_ATTR_S a; memset(&a,0,sizeof(a));
    a.stVencAttr.enType = RK_VIDEO_ID_AVC; a.stVencAttr.enPixelFormat = RK_FMT_YUV420SP;
    a.stVencAttr.u32MaxPicWidth = a.stVencAttr.u32PicWidth = a.stVencAttr.u32VirWidth = IR_W;
    a.stVencAttr.u32MaxPicHeight = a.stVencAttr.u32PicHeight = a.stVencAttr.u32VirHeight = IR_H;
    a.stVencAttr.u32BufSize = IR_W * IR_H * 2; a.stVencAttr.u32StreamBufCnt = 3;
    a.stVencAttr.u32Profile = 100; a.stVencAttr.bByFrame = RK_TRUE;
    a.stRcAttr.enRcMode = VENC_RC_MODE_H264CBR;
    a.stRcAttr.stH264Cbr.u32Gop = gop;
    a.stRcAttr.stH264Cbr.u32SrcFrameRateNum = a.stRcAttr.stH264Cbr.fr32DstFrameRateNum = fps;
    a.stRcAttr.stH264Cbr.u32SrcFrameRateDen = a.stRcAttr.stH264Cbr.fr32DstFrameRateDen = 1;
    a.stRcAttr.stH264Cbr.u32BitRate = bitrate; a.stRcAttr.stH264Cbr.u32StatTime = 1;
    a.stGopAttr.enGopMode = VENC_GOPMODE_NORMALP;
    int rc = RK_MPI_VENC_CreateChn(IR_CHN,&a);
    if (rc != RK_SUCCESS) {fprintf(stderr,"[IR] create encoder failed: %#x\n",rc);return -1;}
    VENC_RECV_PIC_PARAM_S recv = {.s32RecvPicNum = -1};
    rc = RK_MPI_VENC_StartRecvFrame(IR_CHN,&recv);
    if (rc != RK_SUCCESS) {RK_MPI_VENC_DestroyChn(IR_CHN);return -1;}
    return 0;
}
static void run_capture(struct capture *c, MB_POOL pool, LowStream *low) {
    unsigned timeouts=0,encode_failures=0,capture_errors=0;
    uint64_t next_pts=0;
    stream_metrics metrics = {0}; metrics.start=now_us();
    while (!stopped()) {
        struct pollfd pfd = {.fd=c->fd,.events=POLLIN};
        int rc = poll(&pfd,1,500);
        if (rc < 0 && errno == EINTR) continue;
        if (rc < 0 || (pfd.revents & (POLLERR|POLLHUP|POLLNVAL))) break;
        if (!rc) {if (++timeouts >= 10) break;continue;}
        struct v4l2_buffer b = {0};b.type=V4L2_BUF_TYPE_VIDEO_CAPTURE;b.memory=V4L2_MEMORY_MMAP;
        if (xioctl(c->fd,VIDIOC_DQBUF,&b) < 0) {if (errno==EAGAIN) continue;break;}
        timeouts=0;
        MB_BLK block = RK_MPI_MB_GetMB(pool,IR_BYTES,RK_FALSE);
        int converted = -1;
        if (block != MB_INVALID_HANDLE && b.index<c->count && !(b.flags&V4L2_BUF_FLAG_ERROR) &&
            b.bytesused<=c->length[b.index])
            converted=(c->hik ? hik_unpack_nv12 : mino_unpack_nv12)(
                       c->data[b.index],b.bytesused,c->stride,
                       RK_MPI_MB_Handle2VirAddr(block),IR_BYTES,rotate180);
        rc=xioctl(c->fd,VIDIOC_QBUF,&b);
        if (rc<0 || converted<0) {
            if(block!=MB_INVALID_HANDLE) RK_MPI_MB_ReleaseMB(block);
            if(rc<0 || ++capture_errors>=25)break;
            continue;
        }
        capture_errors=0;
        VIDEO_FRAME_INFO_S f;memset(&f,0,sizeof(f));
        f.stVFrame.pMbBlk=block;f.stVFrame.u32Width=f.stVFrame.u32VirWidth=IR_W;
        f.stVFrame.u32Height=f.stVFrame.u32VirHeight=IR_H;
        f.stVFrame.enPixelFormat=RK_FMT_YUV420SP;f.stVFrame.enCompressMode=COMPRESS_MODE_NONE;
        /* Stable 25 Hz media clock; rebase after a real capture stall.
         * Arrival-time jitter must not quantize to duplicate frame timestamps. */
        uint64_t captured_at=now_us();
        if(!next_pts || captured_at>next_pts+200000)next_pts=captured_at;
        f.stVFrame.u64PTS=next_pts;next_pts+=1000000/c->fps;
        rc=RK_MPI_SYS_MmzFlushCache(block,RK_FALSE);
        if(rc==RK_SUCCESS)rc=RK_MPI_VENC_SendFrame(IR_CHN,&f,100);
        low_stream_offer(low, RK_MPI_MB_Handle2VirAddr(block), IR_W, IR_H);
        RK_MPI_MB_ReleaseMB(block);
        if(rc!=RK_SUCCESS) {if(++encode_failures>=10)break;continue;}
        VENC_PACK_S pack;memset(&pack,0,sizeof(pack));
        VENC_STREAM_S stream;memset(&stream,0,sizeof(stream));stream.pstPack=&pack;stream.u32PackCount=1;
        rc=RK_MPI_VENC_GetStream(IR_CHN,&stream,200);
        if(rc!=RK_SUCCESS) {if(++encode_failures>=10)break;continue;}
        encode_failures=0;
        unsigned char *data=RK_MPI_MB_Handle2VirAddr(pack.pMbBlk);
        if(data && pack.u32Len>pack.u32Offset) {
            unsigned length=pack.u32Len-pack.u32Offset;
            uint64_t lock_start=now_us();
            pthread_mutex_lock(server_mutex);
            uint64_t send_start=now_us();
            int tx_rc=rtsp_tx_video(session,data+pack.u32Offset,length,pack.u64PTS);
            int event_rc=rtsp_do_event(server);
            pthread_mutex_unlock(server_mutex);
            uint64_t send_end=now_us();
            stream_metrics_add(&metrics,send_end,length,send_start-lock_start,
                               send_end-send_start,tx_rc<0 || event_rc<0);
        }
        RK_MPI_VENC_ReleaseStream(IR_CHN,&stream);
        stream_metrics_report(&metrics,"IR",now_us());
    }
}
static void *ir_worker(void *unused) {
    (void)unused;prctl(PR_SET_NAME,"mino_ir",0,0,0);
    LowStream *low=low_stream_create("192.168.144.1",5610,IR_W,IR_H);
    while(!stopped()) {
        struct capture c;
        if(capture_open(&c)==0) {
            MB_POOL_CONFIG_S conf;memset(&conf,0,sizeof(conf));
            conf.u64MBSize=IR_BYTES;conf.u32MBCnt=4;conf.bPreAlloc=RK_TRUE;
            conf.enAllocType=MB_ALLOC_TYPE_DMA;conf.enRemapMode=MB_REMAP_MODE_CACHED;
            MB_POOL pool=RK_MPI_MB_CreatePool(&conf);
            if(pool!=MB_INVALID_POOLID) {
                if(encoder_open(c.fps)==0) {
                    fprintf(stderr,"[IR] /live/1 H264 640x512 @%dfps %dkbps GOP=%d\n",c.fps,bitrate,gop);
                    run_capture(&c,pool,low);
                    RK_MPI_VENC_StopRecvFrame(IR_CHN);
                    RK_MPI_VENC_DestroyChn(IR_CHN);
                }
                RK_MPI_MB_DestroyPool(pool);
            }
            capture_close(&c);
        }
        if(!stopped()) fprintf(stderr,"[IR] retry in 5s; RGB continues\n");
        for(int i=0;i<50 && !stopped();++i)usleep(100000);
    }
    low_stream_destroy(low);
    return NULL;
}
int mino_ir_start(rtsp_demo_handle demo,pthread_mutex_t *mutex) {
    if(worker_started)return 0;
    const char *enabled=getenv("HUST_IR_ENABLED");
    if(enabled && !strcmp(enabled,"0"))return 0;
    const char *path=getenv("HUST_IR_DEVICE");
    if(path && strlen(path)>=sizeof(device))return -1;
    snprintf(device,sizeof(device),"%s",path?path:"");
    const char *rot=getenv("HUST_IR_ROTATE");
    if(rot && strcmp(rot,"0") && strcmp(rot,"180"))return -1;
    rotate180=rot && !strcmp(rot,"180");bitrate=1500;gop=50;
    const char *rate=getenv("HUST_IR_BITRATE");
    if(rate && stream_parse_int(rate,128,8192,&bitrate)) {
        fprintf(stderr,"[IR] invalid HUST_IR_BITRATE (128..8192)\n");return -1;
    }
    const char *gop_env=getenv("HUST_IR_GOP");
    if(gop_env && stream_parse_int(gop_env,1,65536,&gop)) {
        fprintf(stderr,"[IR] invalid HUST_IR_GOP (1..65536)\n");return -1;
    }
    server=demo;server_mutex=mutex;
    pthread_mutex_lock(mutex);
    session=rtsp_new_session(demo,"/live/1");
    if(session) {rtsp_set_video(session,RTSP_CODEC_ID_VIDEO_H264,NULL,0);rtsp_sync_video_ts(session,rtsp_get_reltime(),rtsp_get_ntptime());}
    pthread_mutex_unlock(mutex);
    if(!session)return -1;
    __atomic_store_n(&stopping,0,__ATOMIC_RELEASE);
    if(pthread_create(&worker,NULL,ir_worker,NULL)) {
        pthread_mutex_lock(mutex);rtsp_del_session(session);session=NULL;pthread_mutex_unlock(mutex);return -1;
    }
    worker_started=1;return 0;
}
void mino_ir_stop(void) {
    if(!worker_started)return;
    __atomic_store_n(&stopping,1,__ATOMIC_RELEASE);pthread_join(worker,NULL);worker_started=0;
    pthread_mutex_lock(server_mutex);rtsp_del_session(session);session=NULL;pthread_mutex_unlock(server_mutex);
}
