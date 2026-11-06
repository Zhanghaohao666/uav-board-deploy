#define _GNU_SOURCE
#include "rga_safe.h"
#include <rga/im2d.h>
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/dma-buf.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

struct allocation { uint64_t len; uint32_t fd,flags; uint64_t heap_flags; };
static struct { int open,heap,imported; void *pixels; size_t bytes; } fds[512];
static int next_fd=10, fail_alloc, fail_import, fail_submit, fail_sync, fail_config;
static int live, imports, releases, submits;
static _Thread_local int core_configured;
IM_STATUS imconfig(IM_CONFIG_NAME name,uint64_t value)
{
    assert(name==IM_CONFIG_SCHEDULER_CORE && value==3);
    if(fail_config)return IM_STATUS_FAILED;
    core_configured=1;return IM_STATUS_SUCCESS;
}

int __wrap_open(const char *name,int flags,...)
{
    assert(strstr(name,"/dev/dma_heap/")==name);
    assert(!strcmp(name,"/dev/dma_heap/system-dma32") ||
           !strcmp(name,"/dev/dma_heap/system-uncached-dma32"));
    assert(flags & O_CLOEXEC);
    int fd=next_fd++;assert(fd<512);fds[fd].open=fds[fd].heap=1;++live;return fd;
}
int __wrap_close(int fd)
{
    assert(fd>=0 && fd<512 && fds[fd].open && !fds[fd].imported);
    fds[fd].open=0;--live;return 0;
}
int __wrap_ioctl(int fd,unsigned long request,...)
{
    va_list ap;va_start(ap,request);void *pointer=va_arg(ap,void*);va_end(ap);
    assert(fds[fd].open);
    if(request==DMA_BUF_IOCTL_SYNC) {
        assert(!fds[fd].heap);
        if(fail_sync){errno=EIO;return -1;}
        return 0;
    }
    assert(fds[fd].heap && _IOC_TYPE(request)=='H' && _IOC_NR(request)==0);
    if(fail_alloc){errno=ENOMEM;return -1;}
    struct allocation *a=pointer;assert(a->flags & O_CLOEXEC);
    int fresh=next_fd++;assert(fresh<512);a->fd=fresh;
    fds[fresh].open=1;fds[fresh].bytes=a->len;++live;return 0;
}
void *__wrap_mmap(void *address,size_t bytes,int prot,int flags,int fd,off_t offset)
{
    (void)address;(void)prot;(void)flags;assert(!offset && fds[fd].open && !fds[fd].heap);
    assert(bytes==fds[fd].bytes);void *p=malloc(bytes);assert(p);fds[fd].pixels=p;return p;
}
int __wrap_munmap(void *p,size_t bytes)
{
    for(int fd=0;fd<next_fd;++fd)if(fds[fd].pixels==p){
        assert(!fds[fd].imported && bytes==fds[fd].bytes);
        free(p);fds[fd].pixels=NULL;return 0;
    }
    assert(0);return -1;
}
rga_buffer_handle_t importbuffer_fd(int fd,im_handle_param_t *p)
{
    assert(fds[fd].open && !fds[fd].heap && fds[fd].pixels);
    assert(!(p->width%16) && !(p->height%16));
    if(fail_import)return 0;
    ++imports;fds[fd].imported=1;return fd;
}
IM_STATUS releasebuffer_handle(rga_buffer_handle_t h)
{
    assert(fds[h].imported);fds[h].imported=0;++releases;return IM_STATUS_SUCCESS;
}
rga_buffer_t wrapbuffer_handle_t(rga_buffer_handle_t h,int w,int height,int ws,int hs,int fmt)
{
    assert(fds[h].imported);
    return (rga_buffer_t){.width=w,.height=height,.wstride=ws,.hstride=hs,.format=fmt,.handle=h};
}
void imsetColorSpace(rga_buffer_t *p,int cs){p->color_space_mode=cs;}
IM_STATUS imcheck_t(rga_buffer_t a,rga_buffer_t b,rga_buffer_t pat,im_rect ar,im_rect br,im_rect pr,int usage)
{
    (void)pat;(void)ar;(void)br;(void)pr;assert(usage&IM_SYNC);
    assert(core_configured && a.handle && b.handle && a.handle!=b.handle && !a.vir_addr && !b.vir_addr);
    assert(!(a.wstride%16) && !(a.hstride%16) && !(b.wstride%16) && !(b.hstride%16));
    return IM_STATUS_NOERROR;
}
static IM_STATUS copy(rga_buffer_t a,rga_buffer_t b,int sync)
{
    ++submits;assert(sync==1);
    if(fail_submit)return IM_STATUS_FAILED;
    assert(a.format==b.format && a.format==RK_FORMAT_YCbCr_420_SP);
    unsigned char *src=fds[a.handle].pixels,*dst=fds[b.handle].pixels;
    for(int y=0;y<b.height;++y)for(int x=0;x<b.width;++x)
        dst[(size_t)y*b.wstride+x]=src[(size_t)(y*a.height/b.height)*a.wstride+x*a.width/b.width];
    src+=(size_t)a.wstride*a.hstride;dst+=(size_t)b.wstride*b.hstride;
    for(int y=0;y<b.height/2;++y)for(int x=0;x<b.width;++x)
        dst[(size_t)y*b.wstride+x]=src[(size_t)(y*a.height/b.height)*a.wstride+x*a.width/b.width];
    return IM_STATUS_SUCCESS;
}
IM_STATUS imcopy_t(rga_buffer_t a,rga_buffer_t b,int sync){return copy(a,b,sync);}
IM_STATUS imresize_t(rga_buffer_t a,rga_buffer_t b,double x,double y,int interpolation,int sync)
{(void)x;(void)y;(void)interpolation;return copy(a,b,sync);}
IM_STATUS imcvtcolor_t(rga_buffer_t a,rga_buffer_t b,int sf,int df,int cs,int sync)
{(void)a;(void)b;(void)sf;(void)df;(void)cs;(void)sync;assert(0);return -1;}
IM_STATUS imrotate_t(rga_buffer_t a,rga_buffer_t b,int rotation,int sync)
{(void)a;(void)b;(void)rotation;(void)sync;assert(0);return -1;}

static UavRgaLayout layout={18,6,22,8,RK_FORMAT_YCbCr_420_SP};
static unsigned char src[264],dst[264+16];
static int run(void)
{
    return uav_rga_run(src,layout,dst,layout,UAV_RGA_COPY,0,0);
}
static void *on_thread(void *unused)
{(void)unused;assert(run()==0);return NULL;}
int main(int argc,char **argv)
{
    assert(argc==2);memset(src,0xee,sizeof(src));memset(dst,0xab,sizeof(dst));
    for(int y=0;y<6;++y)for(int x=0;x<18;++x)src[y*22+x]=(unsigned char)(x+y*3);
    for(int y=0;y<3;++y)for(int x=0;x<18;++x)src[22*8+y*22+x]=(unsigned char)(90+x+y*5);
    if(!strcmp(argv[1],"padding")) {
        assert(run()==0);
        for(int y=0;y<6;++y)for(int x=0;x<18;++x)assert(src[y*22+x]==dst[y*22+x]);
        for(int y=0;y<3;++y)for(int x=0;x<18;++x)assert(src[22*8+y*22+x]==dst[22*8+y*22+x]);
        for(size_t i=264;i<sizeof(dst);++i)assert(dst[i]==0xab);
        assert(imports==2 && submits==1);
    } else if(!strcmp(argv[1],"reuse")) {
        for(int i=0;i<100;++i)assert(run()==0);
        assert(imports==2 && submits==100);
    } else if(!strcmp(argv[1],"invalid")) {
        UavRgaLayout bad=layout;bad.height=5;
        assert(uav_rga_run(src,bad,dst,layout,UAV_RGA_COPY,0,0)<0);
        bad=layout;bad.wstride=16;
        assert(uav_rga_run(src,bad,dst,layout,UAV_RGA_COPY,0,0)<0);
        assert(!imports && !submits && !live);
    } else if(!strcmp(argv[1],"core")) {
        fail_config=1;assert(run()<0);assert(!live && !imports && !submits);
        fail_config=0;assert(run()<0);assert(!submits && !imports);
    } else if(!strcmp(argv[1],"alloc")) {
        fail_alloc=1;assert(run()<0);assert(!live && !imports && !submits);
        fail_alloc=0;assert(run()<0);assert(!submits);
    } else if(!strcmp(argv[1],"import")) {
        fail_import=1;assert(run()<0);assert(!live && !imports && !submits);
    } else if(!strcmp(argv[1],"submit")) {
        fail_submit=1;assert(run()<0);int n=submits;
        fail_submit=0;assert(run()<0);assert(submits==n);
        for(size_t i=0;i<sizeof(dst);++i)assert(dst[i]==0xab);
    } else if(!strcmp(argv[1],"sync")) {
        fail_sync=1;assert(run()<0);assert(!submits);
    } else if(!strcmp(argv[1],"thread")) {
        assert(run()==0);int n=live;
        pthread_t thread;assert(pthread_create(&thread,NULL,on_thread,NULL)==0);
        assert(pthread_join(thread,NULL)==0);assert(live==n);
    } else if(!strcmp(argv[1],"lru")) {
        unsigned char large[4096]={0};
        for(int n=1;n<=12;++n) {
            UavRgaLayout shape={n*16,4,n*16,4,RK_FORMAT_YCbCr_420_SP};
            assert(uav_rga_run(large,shape,large+2048,shape,UAV_RGA_COPY,0,0)==0);
            assert(live<=8);
        }
    } else if(!strcmp(argv[1],"cycles")) {
        for(int i=0;i<30;++i){assert(run()==0);uav_rga_release_thread();assert(!live);}
    } else assert(0);
    uav_rga_release_thread();assert(!live && imports==releases);
    puts("ok");return 0;
}
