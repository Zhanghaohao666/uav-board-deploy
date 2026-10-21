/* RK3576 Rockit/VENC adapter for the common on-demand low-stream worker.
 * Keep channels 0/1 and their current algorithm/IR pipelines untouched. */
#include "mpp_encoder.h"
#include "rk_mpi_sys.h"
#include "rk_mpi_mb.h"
#include "rk_mpi_venc.h"
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
struct MppEncoder {EncConfig conf;int channel,created,stride,vstride;MB_POOL pool;unsigned char *output;size_t capacity;};
static pthread_mutex_t channels_mutex=PTHREAD_MUTEX_INITIALIZER;
static unsigned used_channels;
static int acquire_channel(void) {
    int channel=-1;pthread_mutex_lock(&channels_mutex);
    for(int i=2;i<8;++i)if(!(used_channels&(1u<<i))){used_channels|=1u<<i;channel=i;break;}
    pthread_mutex_unlock(&channels_mutex);return channel;
}
void enc_destroy(MppEncoder *e) {
    if(!e)return;
    if(e->created){RK_MPI_VENC_StopRecvFrame(e->channel);RK_MPI_VENC_DestroyChn(e->channel);}
    if(e->pool!=MB_INVALID_POOLID)RK_MPI_MB_DestroyPool(e->pool);
    if(e->channel>=0){pthread_mutex_lock(&channels_mutex);used_channels&=~(1u<<e->channel);pthread_mutex_unlock(&channels_mutex);}
    free(e->output);free(e);
}
MppEncoder *enc_create(const EncConfig *cfg) {
    if(!cfg || cfg->codec!=ENC_CODEC_H264 || cfg->in_fmt!=ENC_FMT_NV12)return NULL;
    MppEncoder *e=calloc(1,sizeof(*e));if(!e)return NULL;
    e->conf=*cfg;e->stride=(cfg->width+15)&~15;e->vstride=(cfg->height+15)&~15;
    e->pool=MB_INVALID_POOLID;e->channel=acquire_channel();if(e->channel<0)goto fail;
    VENC_CHN_ATTR_S a;memset(&a,0,sizeof(a));
    a.stVencAttr.enType=RK_VIDEO_ID_AVC;a.stVencAttr.enPixelFormat=RK_FMT_YUV420SP;
    a.stVencAttr.u32MaxPicWidth=a.stVencAttr.u32PicWidth=cfg->width;
    a.stVencAttr.u32MaxPicHeight=a.stVencAttr.u32PicHeight=cfg->height;
    a.stVencAttr.u32VirWidth=e->stride;a.stVencAttr.u32VirHeight=e->vstride;
    a.stVencAttr.u32BufSize=cfg->width*cfg->height*2;
    a.stVencAttr.u32StreamBufCnt=3;a.stVencAttr.u32Profile=100;a.stVencAttr.bByFrame=RK_TRUE;
    a.stRcAttr.enRcMode=VENC_RC_MODE_H264CBR;
    a.stRcAttr.stH264Cbr.u32Gop=cfg->gop;
    a.stRcAttr.stH264Cbr.u32SrcFrameRateNum=a.stRcAttr.stH264Cbr.fr32DstFrameRateNum=cfg->fps;
    a.stRcAttr.stH264Cbr.u32SrcFrameRateDen=a.stRcAttr.stH264Cbr.fr32DstFrameRateDen=1;
    a.stRcAttr.stH264Cbr.u32BitRate=cfg->bitrate_kbps;a.stRcAttr.stH264Cbr.u32StatTime=1;
    a.stGopAttr.enGopMode=VENC_GOPMODE_NORMALP;
    if(RK_MPI_VENC_CreateChn(e->channel,&a)!=RK_SUCCESS)goto fail;
    e->created=1;VENC_RECV_PIC_PARAM_S recv={.s32RecvPicNum=-1};
    if(RK_MPI_VENC_StartRecvFrame(e->channel,&recv)!=RK_SUCCESS)goto fail;
    MB_POOL_CONFIG_S pool;memset(&pool,0,sizeof(pool));
    pool.u64MBSize=(size_t)e->stride*e->vstride*3/2;pool.u32MBCnt=3;pool.bPreAlloc=RK_TRUE;
    pool.enAllocType=MB_ALLOC_TYPE_DMA;pool.enRemapMode=MB_REMAP_MODE_CACHED;
    e->pool=RK_MPI_MB_CreatePool(&pool);if(e->pool==MB_INVALID_POOLID)goto fail;
    return e;
fail:
    fprintf(stderr,"[low-venc] create failed channel=%d\n",e->channel);enc_destroy(e);return NULL;
}
void enc_request_keyframe(MppEncoder *e){if(e)RK_MPI_VENC_RequestIDR(e->channel,RK_FALSE);}
int enc_encode(MppEncoder *e,const void *data,size_t len,const uint8_t **out,size_t *out_len,int *key) {
    if(!e || !data || len<(size_t)e->conf.width*e->conf.height*3/2)return -1;
    size_t dma_bytes=(size_t)e->stride*e->vstride*3/2;
    MB_BLK block=RK_MPI_MB_GetMB(e->pool,dma_bytes,RK_FALSE);if(block==MB_INVALID_HANDLE)return -1;
    void *address=RK_MPI_MB_Handle2VirAddr(block);if(!address){RK_MPI_MB_ReleaseMB(block);return -1;}
    unsigned char *dst=address;const unsigned char *src=data;
    memset(dst,0,dma_bytes);
    for(int y=0;y<e->conf.height;++y)memcpy(dst+(size_t)y*e->stride,src+(size_t)y*e->conf.width,e->conf.width);
    for(int y=0;y<e->conf.height/2;++y)memcpy(dst+(size_t)e->stride*e->vstride+(size_t)y*e->stride,src+(size_t)e->conf.width*e->conf.height+(size_t)y*e->conf.width,e->conf.width);
    VIDEO_FRAME_INFO_S frame;memset(&frame,0,sizeof(frame));
    frame.stVFrame.pMbBlk=block;
    frame.stVFrame.u32Width=e->conf.width;frame.stVFrame.u32VirWidth=e->stride;
    frame.stVFrame.u32Height=e->conf.height;frame.stVFrame.u32VirHeight=e->vstride;
    frame.stVFrame.enPixelFormat=RK_FMT_YUV420SP;frame.stVFrame.enCompressMode=COMPRESS_MODE_NONE;
    struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);frame.stVFrame.u64PTS=(uint64_t)t.tv_sec*1000000+t.tv_nsec/1000;
    int rc=RK_MPI_SYS_MmzFlushCache(block,RK_FALSE);
    if(rc==RK_SUCCESS)rc=RK_MPI_VENC_SendFrame(e->channel,&frame,100);
    RK_MPI_MB_ReleaseMB(block);if(rc!=RK_SUCCESS)return -1;
    VENC_PACK_S pack;memset(&pack,0,sizeof(pack));
    VENC_STREAM_S stream;memset(&stream,0,sizeof(stream));stream.pstPack=&pack;stream.u32PackCount=1;
    if(RK_MPI_VENC_GetStream(e->channel,&stream,200)!=RK_SUCCESS)return -1;
    const uint8_t *ptr=RK_MPI_MB_Handle2VirAddr(pack.pMbBlk);
    size_t bytes=pack.u32Len>=pack.u32Offset?pack.u32Len-pack.u32Offset:0;
    if(bytes>e->capacity){void *p=realloc(e->output,bytes);if(p){e->output=p;e->capacity=bytes;}}
    rc=-1;
    if(ptr && bytes && e->capacity>=bytes){memcpy(e->output,ptr+pack.u32Offset,bytes);*out=e->output;*out_len=bytes;if(key)*key=0;rc=0;}
    RK_MPI_VENC_ReleaseStream(e->channel,&stream);return rc;
}
