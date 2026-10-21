#define _GNU_SOURCE
#include "low_stream.h"
#include "mpp_encoder.h"
#include "rtp_sender.h"
#include <arpa/inet.h>
#include <errno.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include <rga/im2d.h>
#include <rga/rga.h>

#define LEASE_US 5000000ULL
struct LowStream {
    int width, height, low_w, low_h, fps, bitrate, port, control, fd, wake, rgb;
    char host[64];
    struct in_addr peer;
    pthread_t thread;
    pthread_mutex_t mutex;
    int running, wanted, pending;
    uint8_t *mailbox;
    uint64_t frame_us, next_offer_us;
};
static uint64_t mono_us(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec*1000000ULL+t.tv_nsec/1000;
}
static int setting(const char *name, int fallback, int min, int max) {
    const char *s=getenv(name); char *end=NULL;
    if (!s || !*s) return fallback;
    long v=strtol(s,&end,10);
    return end && !*end && v>=min && v<=max ? (int)v : fallback;
}
static void affinity(void) {
    const char *v=getenv("UAV_LOW_CPUS");
    if (!v || !*v) return;
    cpu_set_t mask; CPU_ZERO(&mask);
    int first=0,last=0;
    if (sscanf(v,"%d-%d",&first,&last)!=2) last=first=atoi(v);
    if (first<0 || last<first || last>=CPU_SETSIZE) return;
    for(int i=first;i<=last;++i)CPU_SET(i,&mask);
    pthread_setaffinity_np(pthread_self(),sizeof(mask),&mask);
}
static int scale_nv12(LowStream *s, void *src, void *dst, void *rgb_small) {
    int fmt=s->rgb==2?RK_FORMAT_BGR_888:s->rgb?RK_FORMAT_RGB_888:RK_FORMAT_YCbCr_420_SP;
    rga_buffer_t a=wrapbuffer_virtualaddr(src,s->width,s->height,fmt);
    rga_buffer_t b=wrapbuffer_virtualaddr(dst,s->low_w,s->low_h,RK_FORMAT_YCbCr_420_SP);
    a.wstride=s->width; a.hstride=s->height;
    b.wstride=s->low_w; b.hstride=s->low_h;
    IM_STATUS rc;
    if(s->rgb) {
        rga_buffer_t rgb=wrapbuffer_virtualaddr(rgb_small,s->low_w,s->low_h,fmt);
        rc=imresize(a,rgb);
        if(rc!=IM_STATUS_SUCCESS && rc!=IM_STATUS_NOERROR)return -1;
        rc=imcvtcolor(rgb,b,fmt,RK_FORMAT_YCbCr_420_SP,IM_RGB_TO_YUV_BT601_FULL);
    } else rc=imresize(a,b);
    return rc==IM_STATUS_SUCCESS || rc==IM_STATUS_NOERROR ? 0 : -1;
}
static void clear_slot(LowStream *s) {
    __atomic_store_n(&s->wanted,0,__ATOMIC_RELEASE);
    pthread_mutex_lock(&s->mutex);
    s->pending=0;
    free(s->mailbox); s->mailbox=NULL;
    pthread_mutex_unlock(&s->mutex);
}
static void *worker(void *arg) {
    LowStream *s=arg; affinity();
    MppEncoder *enc=NULL; RtpSender *rtp=NULL;
    uint8_t *work=NULL,*small=NULL,*rgb_small=NULL;
    const size_t bytes=(size_t)s->width*s->height*(s->rgb?3:3)/(s->rgb?1:2);
    const size_t low_bytes=(size_t)s->low_w*s->low_h*3/2;
    uint64_t lease=0,retry=0,epoch=mono_us(),reported=0,frames=0;
    int key_requested=0;
    while (__atomic_load_n(&s->running,__ATOMIC_ACQUIRE)) {
        struct pollfd fds[2]={{s->fd,POLLIN,0},{s->wake,POLLIN,0}};
        int ready=poll(fds,2,500);
        if (ready<0 && errno!=EINTR)break;
        if (fds[1].revents&POLLIN) {uint64_t n;while(read(s->wake,&n,sizeof(n))>0){}}
        if (fds[0].revents&POLLIN) {
            char text[64]; struct sockaddr_in from; socklen_t len=sizeof(from);
            ssize_t n;
            while((n=recvfrom(s->fd,text,sizeof(text)-1,MSG_DONTWAIT,(struct sockaddr*)&from,&len))>0) {
                if (from.sin_addr.s_addr!=s->peer.s_addr)continue;
                text[n]=0;
                if (!strcmp(text,"UAV_LOW_V1") || !strcmp(text,"UAV_LOW_KEY_V1")) {
                    lease=mono_us();
                    if (!strcmp(text,"UAV_LOW_KEY_V1"))key_requested=1;
                }
                len=sizeof(from);
            }
        }
        uint64_t now=mono_us();
        if (!lease || now-lease>=LEASE_US) {
            if(enc) {
                clear_slot(s); enc_destroy(enc); enc=NULL;
                rtp_close(rtp);rtp=NULL;free(work);work=NULL;free(small);small=NULL;
                free(rgb_small);rgb_small=NULL;
                fprintf(stderr,"[low %d] idle: encoder stopped after lease expiry\n",s->port);
            }
            continue;
        }
        if(!enc && now>=retry) {
            EncConfig cfg={.width=s->low_w,.height=s->low_h,.in_fmt=ENC_FMT_NV12,
                .codec=ENC_CODEC_H264,.rc_mode=ENC_RC_CBR,.fps=s->fps,
                .bitrate_kbps=s->bitrate,.bitrate_max_kbps=s->bitrate,.gop=s->fps};
            work=malloc(bytes);small=malloc(low_bytes);
            if(s->rgb)rgb_small=malloc((size_t)s->low_w*s->low_h*3);
            uint8_t *mail=malloc(bytes);
            enc=enc_create(&cfg);rtp=rtp_open(s->host,s->port,0);
            if(!work || !small || !mail || !enc || !rtp || (s->rgb && !rgb_small)) {
                if(enc)enc_destroy(enc);
                enc=NULL;
                if(rtp)rtp_close(rtp);
                rtp=NULL;
                free(work);work=NULL;free(small);small=NULL;free(mail);
                free(rgb_small);rgb_small=NULL;
                retry=now+2000000ULL;
                fprintf(stderr,"[low %d] resource unavailable; retry later (main stream unaffected)\n",s->port);
                continue;
            }
            pthread_mutex_lock(&s->mutex);s->mailbox=mail;s->pending=0;
            __atomic_store_n(&s->wanted,1,__ATOMIC_RELEASE);pthread_mutex_unlock(&s->mutex);
            key_requested=1;reported=now;frames=0;
            fprintf(stderr,"[low %d] active: %dx%d %dfps %dkbps control=%d\n",s->port,s->low_w,s->low_h,s->fps,s->bitrate,s->control);
        }
        if(!enc)continue;
        uint64_t frame_us=0;
        pthread_mutex_lock(&s->mutex);
        if(s->pending) {
            uint8_t *tmp=work;work=s->mailbox;s->mailbox=tmp;
            s->pending=0;frame_us=s->frame_us;
        }
        pthread_mutex_unlock(&s->mutex);
        if(!frame_us || mono_us()-frame_us>200000ULL)continue;
        if(scale_nv12(s,work,small,rgb_small)) {
            if(now-retry>=2000000ULL)fprintf(stderr,"[low %d] hardware scale failed\n",s->port);
            retry=now;continue;
        }
        if(key_requested){enc_request_keyframe(enc);key_requested=0;}
        const uint8_t *out;size_t count;int key;
        if(!enc_encode(enc,small,low_bytes,&out,&count,&key)) {
            rtp_send_annexb(rtp,out,count,(uint32_t)((frame_us-epoch)*90ULL/1000ULL));
            ++frames;
        }
        if(now-reported>=10000000ULL) {
            fprintf(stderr,"[low %d] %.2f fps (latest-frame slot)\n",s->port,frames*1e6/(now-reported));
            reported=now;frames=0;
        }
    }
    clear_slot(s);
    if(enc)enc_destroy(enc);
    if(rtp)rtp_close(rtp);
    free(work);free(small);free(rgb_small);
    return NULL;
}
static LowStream *create(const char *host,int main_port,int width,int height,int rgb) {
    const char *enabled=getenv("UAV_LOW_ENABLE");
    if(!enabled || strcmp(enabled,"1"))return NULL;
    const char *bind_ip=getenv("UAV_LOW_BIND"),*peer=getenv("UAV_LOW_PEER");
    if(!bind_ip || !peer || width<=0 || height<=0 || (width&1) || (height&1))return NULL;
    LowStream *s=calloc(1,sizeof(*s));if(!s)return NULL;
    s->fd=s->wake=-1;s->width=width;s->height=height;s->rgb=rgb;
    s->low_w=setting("UAV_LOW_WIDTH",width<=640?320:640,64,width);
    s->low_w&=~15;
    s->low_h=setting("UAV_LOW_HEIGHT",(s->low_w*height/width)&~1,32,height)&~1;
    s->fps=setting("UAV_LOW_FPS",15,1,30);
    s->bitrate=setting("UAV_LOW_BITRATE",width<=640?400:650,128,2000);
    s->port=setting("UAV_LOW_RTP_PORT",main_port+1000,1024,65535);
    s->control=setting("UAV_LOW_CONTROL_PORT",main_port+10000,1024,65535);
    snprintf(s->host,sizeof(s->host),"%s",host);
    struct sockaddr_in addr={.sin_family=AF_INET,.sin_port=htons(s->control)};
    if(inet_pton(AF_INET,bind_ip,&addr.sin_addr)!=1 || inet_pton(AF_INET,peer,&s->peer)!=1)goto fail;
    s->fd=socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);
    s->wake=eventfd(0,EFD_CLOEXEC|EFD_NONBLOCK);
    if(s->fd<0 || s->wake<0 || bind(s->fd,(struct sockaddr*)&addr,sizeof(addr)))goto fail;
    pthread_mutex_init(&s->mutex,NULL);s->running=1;
    if(pthread_create(&s->thread,NULL,worker,s)){pthread_mutex_destroy(&s->mutex);goto fail;}
    fprintf(stderr,"[low %d] ready, encoder OFF; lease peer=%s bind=%s:%d\n",s->port,peer,bind_ip,s->control);
    return s;
fail:
    fprintf(stderr,"[low] disabled: invalid configuration or control bind failure (%s)\n",strerror(errno));
    if(s->fd>=0)close(s->fd);
    if(s->wake>=0)close(s->wake);
    free(s);return NULL;
}
LowStream *low_stream_create(const char *host,int main_port,int width,int height) {return create(host,main_port,width,height,0);}
LowStream *low_stream_create_rgb(const char *host,int main_port,int width,int height,int bgr) {return create(host,main_port,width,height,bgr?2:1);}
void low_stream_offer(LowStream *s,const void *nv12,int stride,int vstride) {
    if(!s || !nv12 || stride<s->width || vstride<s->height || !__atomic_load_n(&s->wanted,__ATOMIC_ACQUIRE))return;
    uint64_t now=mono_us(),period=1000000ULL/s->fps;
    if(now<s->next_offer_us)return;
    if(pthread_mutex_trylock(&s->mutex))return;
    if(!__atomic_load_n(&s->wanted,__ATOMIC_ACQUIRE) || !s->mailbox){pthread_mutex_unlock(&s->mutex);return;}
    const uint8_t *src=nv12;
    if(s->rgb) {
        for(int y=0;y<s->height;++y)memcpy(s->mailbox+(size_t)y*s->width*3,src+(size_t)y*stride*3,(size_t)s->width*3);
    } else {
        for(int y=0;y<s->height;++y)memcpy(s->mailbox+(size_t)y*s->width,src+(size_t)y*stride,s->width);
        size_t dst_uv=(size_t)s->width*s->height,src_uv=(size_t)stride*vstride;
        for(int y=0;y<s->height/2;++y)memcpy(s->mailbox+dst_uv+(size_t)y*s->width,src+src_uv+(size_t)y*stride,s->width);
    }
    s->frame_us=now;s->pending=1;
    if(!s->next_offer_us || now-s->next_offer_us>period)s->next_offer_us=now;
    s->next_offer_us+=period;
    pthread_mutex_unlock(&s->mutex);
    uint64_t one=1;ssize_t written=write(s->wake,&one,sizeof(one));(void)written;
}
void low_stream_destroy(LowStream *s) {
    if(!s)return;
    __atomic_store_n(&s->running,0,__ATOMIC_RELEASE);
    uint64_t one=1;ssize_t written=write(s->wake,&one,sizeof(one));(void)written;
    pthread_join(s->thread,NULL);pthread_mutex_destroy(&s->mutex);
    close(s->fd);close(s->wake);free(s);
}
