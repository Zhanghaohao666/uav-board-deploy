/* Synthetic NV12 input: hardware integration test without opening a camera. */
#include "low_stream.h"
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static volatile sig_atomic_t running=1;
static void stop(int sig){(void)sig;running=0;}
int main(void){
    signal(SIGTERM,stop);signal(SIGINT,stop);
    const int w=1920,h=1080;
    size_t bytes=(size_t)w*h*3/2;
    uint8_t *image=malloc(bytes);if(!image)return 1;
    memset(image+w*h,128,bytes-w*h);
    LowStream *low=low_stream_create("127.0.0.1",20000,w,h);
    if(!low){free(image);return 2;}
    for(unsigned frame=0;running;++frame){
        memset(image,32,w*h);
        unsigned left=(frame*12)%(w-128);
        for(int y=300;y<700;++y)memset(image+y*w+left,220,128);
        low_stream_offer(low,image,w,h);usleep(33333);
    }
    low_stream_destroy(low);free(image);return 0;
}
