#pragma once

/* All RGA submissions use imported DMA32 handles. Application-owned pointers
 * are copied at this boundary and are never passed to the kernel as userptr. */
#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int width, height, wstride, hstride, format;
} UavRgaLayout;

typedef enum {
    UAV_RGA_COPY, UAV_RGA_RESIZE, UAV_RGA_COLOR, UAV_RGA_ROTATE
} UavRgaOperation;

int uav_rga_run(const void *src, UavRgaLayout input,
                void *dst, UavRgaLayout output,
                UavRgaOperation operation, int rotation, int color_space);

/* Called when an on-demand branch goes idle. Normal camera thread exit also
 * frees its private cache through the pthread key destructor. */
void uav_rga_release_thread(void);

#ifdef __cplusplus
}
#endif
