#pragma once
#include <stdint.h>
/* Minimal test API: these functions never submit work to a real driver. */
typedef unsigned rga_buffer_handle_t;
typedef int IM_STATUS;
enum { IM_STATUS_SUCCESS=1, IM_STATUS_NOERROR=2, IM_STATUS_FAILED=-1 };
typedef enum { IM_CONFIG_SCHEDULER_CORE } IM_CONFIG_NAME;
enum { IM_SCHEDULER_RGA3_CORE0=1, IM_SCHEDULER_RGA3_CORE1=2 };
IM_STATUS imconfig(IM_CONFIG_NAME,uint64_t);
enum { RK_FORMAT_RGB_888=2, RK_FORMAT_BGR_888=7, RK_FORMAT_YCbCr_420_SP=0xa00 };
enum { IM_SYNC=1<<19, INTER_LINEAR=1, IM_COLOR_SPACE_DEFAULT=0,
       IM_YUV_TO_RGB_BT601_FULL=2, IM_RGB_TO_YUV_BT601_FULL=4,
       IM_HAL_TRANSFORM_ROT_90=1, IM_HAL_TRANSFORM_ROT_180=2, IM_HAL_TRANSFORM_ROT_270=4 };
typedef struct { uint32_t width,height,format; } im_handle_param_t;
typedef struct { int x,y,width,height; } im_rect;
typedef struct {
    int width,height,wstride,hstride,format,color_space_mode;
    rga_buffer_handle_t handle;
    void *vir_addr;
    int fd;
} rga_buffer_t;
rga_buffer_handle_t importbuffer_fd(int, im_handle_param_t *);
IM_STATUS releasebuffer_handle(rga_buffer_handle_t);
rga_buffer_t wrapbuffer_handle_t(rga_buffer_handle_t,int,int,int,int,int);
void imsetColorSpace(rga_buffer_t *,int);
IM_STATUS imcheck_t(rga_buffer_t,rga_buffer_t,rga_buffer_t,im_rect,im_rect,im_rect,int);
IM_STATUS imcopy_t(rga_buffer_t,rga_buffer_t,int);
IM_STATUS imresize_t(rga_buffer_t,rga_buffer_t,double,double,int,int);
IM_STATUS imcvtcolor_t(rga_buffer_t,rga_buffer_t,int,int,int,int);
IM_STATUS imrotate_t(rga_buffer_t,rga_buffer_t,int,int);
