#pragma once
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef struct LowStream LowStream;
/* Disabled unless UAV_LOW_ENABLE=1. RTP/control default to main_port+1000/+10000.
 * Bind and peer must explicitly identify the local/wired control network. */
LowStream *low_stream_create(const char *host, int main_port, int width, int height);
LowStream *low_stream_create_rgb(const char *host, int main_port, int width, int height, int bgr);
/* Offer already-drawn NV12. Never waits for the encoder; one latest-frame slot.
 * stride/vstride describe the source allocation, including the UV-plane offset. */
void low_stream_offer(LowStream *, const void *nv12, int stride, int vstride);
void low_stream_destroy(LowStream *);
#ifdef __cplusplus
}
#endif
