#include "mino_unpack.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(void) {
    const size_t stride=2576, bytes=stride*520, size=640*512*3/2;
    uint8_t *src=malloc(bytes), *dst=malloc(size), *rot=malloc(size);
    assert(src && dst && rot);
    memset(src, 0xee, bytes); /* RAW, line padding and metadata must not leak. */
    for (size_t y=0;y<512;++y) for(size_t x=0;x<640;x+=2) {
        uint8_t *p=src+y*stride+1280+2*x;
        p[0]=(y%2)?102:100; p[1]=(uint8_t)(x+y);
        p[2]=(y%2)?152:150; p[3]=(uint8_t)(x+y+1);
    }
    assert(mino_unpack_nv12(src,bytes,stride,dst,size,0)==0);
    assert(mino_unpack_nv12(src,bytes,stride,rot,size,1)==0);
    for(size_t y=0;y<512;++y) for(size_t x=0;x<640;++x) {
        size_t i=y*640+x;
        assert(dst[i]==(uint8_t)(x+y));
        assert(rot[640*512-1-i]==dst[i]);
    }
    for(size_t i=640*512;i<size;i+=2) {
        assert(dst[i]==101 && dst[i+1]==151);
        assert(rot[i]==101 && rot[i+1]==151);
    }
    assert(mino_unpack_nv12(src,100,stride,dst,size,0)<0);
    assert(mino_unpack_nv12(src,bytes,2000,dst,size,0)<0);
    assert(mino_unpack_nv12(src,bytes,stride,dst,size-1,0)<0);
    free(src);free(dst);free(rot);
    puts("PASS: image extraction, metadata exclusion, chroma, stride, rotation, bounds");
}
