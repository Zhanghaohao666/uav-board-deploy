#define _GNU_SOURCE
#include "rga_safe.h"

#include <errno.h>
#include <fcntl.h>
#include <linux/dma-buf.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <rga/im2d.h>
#include <rga/rga.h>

/* Stable Linux dma-heap ABI; some vendor userspace headers omit dma-heap.h. */
struct uav_heap_allocation {
    uint64_t len;
    uint32_t fd, fd_flags;
    uint64_t heap_flags;
};
#define UAV_HEAP_ALLOC _IOWR('H', 0, struct uav_heap_allocation)
#define CACHE_SLOTS 8
#define MAX_IMAGE_BYTES (64U * 1024U * 1024U)

typedef struct {
    int fd;
    void *pixels;
    size_t bytes;
    rga_buffer_handle_t handle;
    UavRgaLayout layout;
    uint64_t touched;
} DmaImage;

typedef struct {
    DmaImage images[CACHE_SLOTS];
    uint64_t tick;
    int blocked, configured;
} ThreadCache;

static pthread_key_t cache_key;
static pthread_once_t cache_once = PTHREAD_ONCE_INIT;
static int cache_key_error;

static int success(IM_STATUS status)
{
    return status == IM_STATUS_SUCCESS || status == IM_STATUS_NOERROR;
}

static void image_close(DmaImage *image)
{
    if (image->handle) releasebuffer_handle(image->handle);
    if (image->pixels) munmap(image->pixels, image->bytes);
    if (image->fd >= 0) close(image->fd);
    memset(image, 0, sizeof(*image));
    image->fd = -1;
}

static void cache_destroy(void *pointer)
{
    ThreadCache *cache = pointer;
    if (!cache) return;
    for (int i = 0; i < CACHE_SLOTS; ++i) image_close(&cache->images[i]);
    free(cache);
}

static void cache_init(void)
{
    cache_key_error = pthread_key_create(&cache_key, cache_destroy);
}

static ThreadCache *get_cache(void)
{
    if (pthread_once(&cache_once, cache_init) || cache_key_error) return NULL;
    ThreadCache *cache = pthread_getspecific(cache_key);
    if (!cache) {
        cache = calloc(1, sizeof(*cache));
        if (!cache) return NULL;
        for (int i = 0; i < CACHE_SLOTS; ++i) cache->images[i].fd = -1;
        if (pthread_setspecific(cache_key, cache)) {
            free(cache);
            return NULL;
        }
    }
    return cache;
}

void uav_rga_release_thread(void)
{
    if (pthread_once(&cache_once, cache_init) || cache_key_error) return;
    ThreadCache *cache = pthread_getspecific(cache_key);
    if (cache) {
        pthread_setspecific(cache_key, NULL);
        cache_destroy(cache);
    }
}

static int layout_bytes(UavRgaLayout layout, size_t *bytes)
{
    if (layout.width <= 0 || layout.height <= 0 ||
        layout.width > 8192 || layout.height > 8192 ||
        layout.wstride < layout.width || layout.hstride < layout.height ||
        layout.wstride > 8192 || layout.hstride > 8192) return -1;
    size_t pixels = (size_t)layout.wstride * layout.hstride;
    if (layout.format == RK_FORMAT_YCbCr_420_SP) {
        if ((layout.width | layout.height | layout.wstride | layout.hstride) & 1)
            return -1;
        *bytes = pixels * 3 / 2;
    } else if (layout.format == RK_FORMAT_RGB_888 ||
               layout.format == RK_FORMAT_BGR_888) {
        *bytes = pixels * 3;
    } else return -1;
    return *bytes <= MAX_IMAGE_BYTES ? 0 : -1;
}

static int same_layout(UavRgaLayout a, UavRgaLayout b)
{
    return a.width == b.width && a.height == b.height &&
           a.wstride == b.wstride && a.hstride == b.hstride && a.format == b.format;
}

static int sync_cpu(DmaImage *image, uint64_t flags)
{
    struct dma_buf_sync sync = {.flags = flags};
    int rc;
    do { rc = ioctl(image->fd, DMA_BUF_IOCTL_SYNC, &sync); }
    while (rc < 0 && (errno == EINTR || errno == EAGAIN));
    return rc;
}

static DmaImage *image_get(ThreadCache *cache, UavRgaLayout layout,
                            DmaImage *exclude)
{
    layout.wstride = (layout.width + 15) & ~15;
    layout.hstride = (layout.height + 15) & ~15;
    size_t bytes;
    if (layout_bytes(layout, &bytes)) return NULL;
    DmaImage *victim = NULL;
    for (int i = 0; i < CACHE_SLOTS; ++i) {
        DmaImage *image = &cache->images[i];
        if (image == exclude) continue;
        if (image->handle && same_layout(image->layout, layout)) {
            image->touched = ++cache->tick;
            return image;
        }
        if (!victim || image->touched < victim->touched) victim = image;
    }
    if (!victim) return NULL;
    /* Do not fall back to the unrestricted system/CMA heap or malloc. */
    const char *heaps[] = {"/dev/dma_heap/system-dma32",
                          "/dev/dma_heap/system-uncached-dma32"};
    struct uav_heap_allocation allocation = {
        .len = (bytes + 4095) & ~(uint64_t)4095,
        .fd_flags = O_RDWR | O_CLOEXEC
    };
    int allocated = 0;
    for (size_t i = 0; i < sizeof(heaps) / sizeof(heaps[0]); ++i) {
        int heap = open(heaps[i], O_RDWR | O_CLOEXEC);
        if (heap < 0) continue;
        int rc;
        do { rc = ioctl(heap, UAV_HEAP_ALLOC, &allocation); }
        while (rc < 0 && errno == EINTR);
        close(heap);
        if (rc == 0) { allocated = 1; break; }
    }
    if (!allocated) return NULL;
    DmaImage next = {.fd = (int)allocation.fd, .bytes = (size_t)allocation.len,
                     .layout = layout};
    next.pixels = mmap(NULL, next.bytes, PROT_READ | PROT_WRITE,
                       MAP_SHARED, next.fd, 0);
    if (next.pixels == MAP_FAILED) {
        next.pixels = NULL;
        image_close(&next);
        return NULL;
    }
    im_handle_param_t parameters = {.width = (uint32_t)layout.wstride,
                                    .height = (uint32_t)layout.hstride,
                                    .format = (uint32_t)layout.format};
    next.handle = importbuffer_fd(next.fd, &parameters);
    if (!next.handle) { image_close(&next); return NULL; }
    next.touched = ++cache->tick;
    image_close(victim);
    *victim = next;
    return victim;
}

/* Copy the visible image and initialize padding. NV12's UV offset uses the
 * allocation's vertical stride, not the visible height. */
static void copy_image(void *destination, UavRgaLayout dst,
                        const void *source, UavRgaLayout src)
{
    uint8_t *out = destination;
    const uint8_t *in = source;
    int bpp = src.format == RK_FORMAT_YCbCr_420_SP ? 1 : 3;
    size_t row = (size_t)src.width * bpp;
    memset(out, 0, (size_t)dst.wstride * dst.hstride * bpp);
    for (int y = 0; y < src.height; ++y)
        memcpy(out + (size_t)y * dst.wstride * bpp,
               in + (size_t)y * src.wstride * bpp, row);
    if (src.format == RK_FORMAT_YCbCr_420_SP) {
        out += (size_t)dst.wstride * dst.hstride;
        in += (size_t)src.wstride * src.hstride;
        memset(out, 128, (size_t)dst.wstride * dst.hstride / 2);
        for (int y = 0; y < src.height / 2; ++y)
            memcpy(out + (size_t)y * dst.wstride,
                   in + (size_t)y * src.wstride, row);
    }
}

int uav_rga_run(const void *src, UavRgaLayout input,
                void *dst, UavRgaLayout output,
                UavRgaOperation operation, int rotation, int color_space)
{
    size_t ignored;
    if (!src || !dst || layout_bytes(input, &ignored) ||
        layout_bytes(output, &ignored) || operation < UAV_RGA_COPY ||
        operation > UAV_RGA_ROTATE) return -1;
    if ((operation == UAV_RGA_COPY || operation == UAV_RGA_RESIZE) && input.format != output.format) return -1;
    if ((operation == UAV_RGA_COPY || operation == UAV_RGA_COLOR) &&
        (input.width != output.width || input.height != output.height)) return -1;
    if (operation == UAV_RGA_ROTATE) {
        if (rotation == IM_HAL_TRANSFORM_ROT_90 || rotation == IM_HAL_TRANSFORM_ROT_270) {
            if (input.width != output.height || input.height != output.width) return -1;
        } else if (rotation == IM_HAL_TRANSFORM_ROT_180) {
            if (input.width != output.width || input.height != output.height) return -1;
        } else return -1;
    }
    ThreadCache *cache = get_cache();
    if (!cache || cache->blocked) return -1;
    /* On this RK3588 BSP the legacy RGA2 core can also time out with valid
     * imported DMA32 buffers. Use the two RGA3 cores explicitly; do not let
     * automatic scheduling send a job back to the affected RGA2 core.
     * imconfig applies to the calling thread, including each low worker. */
    if (!cache->configured) {
        if (!success(imconfig(IM_CONFIG_SCHEDULER_CORE,
                              IM_SCHEDULER_RGA3_CORE0 | IM_SCHEDULER_RGA3_CORE1)))
            goto fail;
        cache->configured = 1;
    }
    DmaImage *in = image_get(cache, input, NULL);
    DmaImage *out = in ? image_get(cache, output, in) : NULL;
    if (!in || !out) goto fail;
    if (sync_cpu(in, DMA_BUF_SYNC_START | DMA_BUF_SYNC_WRITE)) goto fail;
    copy_image(in->pixels, in->layout, src, input);
    if (sync_cpu(in, DMA_BUF_SYNC_END | DMA_BUF_SYNC_WRITE)) goto fail;

    rga_buffer_t a = wrapbuffer_handle_t(in->handle, input.width, input.height,
                                        in->layout.wstride, in->layout.hstride, input.format);
    rga_buffer_t b = wrapbuffer_handle_t(out->handle, output.width, output.height,
                                        out->layout.wstride, out->layout.hstride, output.format);
    if (color_space != IM_COLOR_SPACE_DEFAULT) {
        imsetColorSpace(&a, color_space);
        imsetColorSpace(&b, color_space);
    }
    im_rect rectangle = {0};
    int usage = IM_SYNC | (operation == UAV_RGA_ROTATE ? rotation : 0);
    IM_STATUS rc = imcheck_t(a, b, (rga_buffer_t){0}, rectangle, rectangle, rectangle, usage);
    if (!success(rc)) goto fail;
    switch (operation) {
    case UAV_RGA_COPY: rc = imcopy_t(a, b, 1); break;
    case UAV_RGA_RESIZE: rc = imresize_t(a, b, 0, 0, INTER_LINEAR, 1); break;
    case UAV_RGA_COLOR:
        rc = imcvtcolor_t(a, b, input.format, output.format, color_space, 1); break;
    case UAV_RGA_ROTATE: rc = imrotate_t(a, b, rotation, 1); break;
    default: goto fail;
    }
    if (!success(rc)) goto fail;
    if (sync_cpu(out, DMA_BUF_SYNC_START | DMA_BUF_SYNC_READ)) goto fail;
    copy_image(dst, output, out->pixels, out->layout);
    if (sync_cpu(out, DMA_BUF_SYNC_END | DMA_BUF_SYNC_READ)) goto fail;
    return 0;
fail:
    /* The vendor error cleanup has caused whole-system corruption. Never
     * retry the same failing hardware path at frame rate on this thread. */
    cache->blocked = 1;
    fprintf(stderr, "[rga-safe] RGA3/DMA32 handle operation failed; this thread is blocked (no RGA2/userptr fallback)\n");
    return -1;
}
