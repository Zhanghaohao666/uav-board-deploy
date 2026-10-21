#include "camera.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <linux/videodev2.h>

static int xioctl(int fd, unsigned long req, void *arg)
{
    int r;
    do { r = ioctl(fd, req, arg); } while (r == -1 && errno == EINTR);
    return r;
}

static int device_gone_error(int error)
{
    return error == ENODEV || error == EIO || error == ENXIO ||
           error == EBADF;
}

int cam_open(Camera *cam, const char *dev,
             int width, int height,
             unsigned pixfmt, int buf_count, int fps)
{
    memset(cam, 0, sizeof(*cam));
    cam->fd = -1;
    cam->fd = open(dev, O_RDWR | O_NONBLOCK);
    if (cam->fd < 0) {
        fprintf(stderr, "[cam] open %s failed: %s\n", dev, strerror(errno));
        return -1;
    }
    cam->width  = width;
    cam->height = height;
    cam->pixfmt = pixfmt;

    struct v4l2_capability cap;
    if (xioctl(cam->fd, VIDIOC_QUERYCAP, &cap) < 0) {
        perror("[cam] VIDIOC_QUERYCAP");
        goto err;
    }
    if (!(cap.capabilities & V4L2_CAP_VIDEO_CAPTURE_MPLANE) &&
        !(cap.capabilities & V4L2_CAP_VIDEO_CAPTURE)) {
        fprintf(stderr, "[cam] %s is not a capture device\n", dev);
        goto err;
    }

    cam->use_mplane = (cap.capabilities & V4L2_CAP_VIDEO_CAPTURE_MPLANE) ? 1 : 0;

    if (cam->use_mplane) {
        struct v4l2_format fmt;
        memset(&fmt, 0, sizeof(fmt));
        fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        fmt.fmt.pix_mp.width       = width;
        fmt.fmt.pix_mp.height      = height;
        fmt.fmt.pix_mp.pixelformat = pixfmt;
        fmt.fmt.pix_mp.field       = V4L2_FIELD_NONE;
        fmt.fmt.pix_mp.num_planes  = 1;
        if (xioctl(cam->fd, VIDIOC_S_FMT, &fmt) < 0) {
            perror("[cam] VIDIOC_S_FMT (mplane)");
            goto err;
        }
        cam->width  = fmt.fmt.pix_mp.width;
        cam->height = fmt.fmt.pix_mp.height;
        cam->pixfmt = fmt.fmt.pix_mp.pixelformat;
        printf("[cam] %s: %dx%d fmt=%.4s (mplane)\n", dev,
               cam->width, cam->height, (char*)&fmt.fmt.pix_mp.pixelformat);
    } else {
        struct v4l2_format fmt;
        memset(&fmt, 0, sizeof(fmt));
        fmt.type                = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        fmt.fmt.pix.width       = width;
        fmt.fmt.pix.height      = height;
        fmt.fmt.pix.pixelformat = pixfmt;
        fmt.fmt.pix.field       = V4L2_FIELD_NONE;
        if (xioctl(cam->fd, VIDIOC_S_FMT, &fmt) < 0) {
            perror("[cam] VIDIOC_S_FMT (single)");
            goto err;
        }
        cam->width  = fmt.fmt.pix.width;
        cam->height = fmt.fmt.pix.height;
        cam->pixfmt = fmt.fmt.pix.pixelformat;
        printf("[cam] %s: %dx%d fmt=%.4s (single-plane)\n", dev,
               cam->width, cam->height, (char*)&fmt.fmt.pix.pixelformat);
    }

    if (cam->pixfmt != pixfmt) {
        fprintf(stderr, "[cam] %s requested %.4s but driver selected %.4s\n",
                dev, (char *)&pixfmt, (char *)&cam->pixfmt);
        goto err;
    }

    if (!cam->use_mplane && fps > 0) {
        struct v4l2_streamparm parm;
        memset(&parm, 0, sizeof(parm));
        parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        parm.parm.capture.timeperframe.numerator = 1;
        parm.parm.capture.timeperframe.denominator = fps;
        if (xioctl(cam->fd, VIDIOC_S_PARM, &parm) < 0) {
            fprintf(stderr, "[cam] %s cannot set %d fps: %s\n",
                    dev, fps, strerror(errno));
        }
    }

    if (buf_count > CAM_MAX_BUFS) buf_count = CAM_MAX_BUFS;

    struct v4l2_requestbuffers req;
    memset(&req, 0, sizeof(req));
    req.count  = buf_count;
    req.type   = cam->use_mplane ? V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE
                                 : V4L2_BUF_TYPE_VIDEO_CAPTURE;
    req.memory = V4L2_MEMORY_MMAP;
    if (xioctl(cam->fd, VIDIOC_REQBUFS, &req) < 0) {
        perror("[cam] VIDIOC_REQBUFS");
        goto err;
    }
    if (req.count == 0 || req.count > CAM_MAX_BUFS) {
        fprintf(stderr, "[cam] invalid buffer count returned by driver: %u\n",
                req.count);
        goto err;
    }
    cam->buf_count = req.count;

    for (int i = 0; i < cam->buf_count; i++) {
        struct v4l2_buffer buf;
        struct v4l2_plane  planes[1];
        memset(&buf, 0, sizeof(buf));
        memset(planes, 0, sizeof(planes));
        buf.index  = i;
        buf.type   = req.type;
        buf.memory = V4L2_MEMORY_MMAP;
        if (cam->use_mplane) { buf.m.planes = planes; buf.length = 1; }
        if (xioctl(cam->fd, VIDIOC_QUERYBUF, &buf) < 0) {
            perror("[cam] VIDIOC_QUERYBUF");
            goto err;
        }
        size_t len    = cam->use_mplane ? planes[0].length      : buf.length;
        off_t  offset = cam->use_mplane ? planes[0].m.mem_offset : buf.m.offset;

        cam->bufs[i].length = len;
        cam->bufs[i].start  = mmap(NULL, len, PROT_READ | PROT_WRITE,
                                   MAP_SHARED, cam->fd, offset);
        if (cam->bufs[i].start == MAP_FAILED) {
            perror("[cam] mmap");
            goto err;
        }

        memset(&buf, 0, sizeof(buf));
        memset(planes, 0, sizeof(planes));
        buf.index  = i;
        buf.type   = req.type;
        buf.memory = V4L2_MEMORY_MMAP;
        if (cam->use_mplane) { buf.m.planes = planes; buf.length = 1; }
        if (xioctl(cam->fd, VIDIOC_QBUF, &buf) < 0) {
            perror("[cam] VIDIOC_QBUF");
            goto err;
        }
    }

    enum v4l2_buf_type type = req.type;
    if (xioctl(cam->fd, VIDIOC_STREAMON, &type) < 0) {
        perror("[cam] VIDIOC_STREAMON");
        goto err;
    }

    printf("[cam] %s: stream on, %d buffers\n", dev, cam->buf_count);
    return 0;

err:
    for (int i = 0; i < cam->buf_count; i++) {
        if (cam->bufs[i].start && cam->bufs[i].start != MAP_FAILED) {
            munmap(cam->bufs[i].start, cam->bufs[i].length);
            cam->bufs[i].start = NULL;
            cam->bufs[i].length = 0;
        }
    }
    cam->buf_count = 0;
    if (cam->fd >= 0) close(cam->fd);
    cam->fd = -1;
    return -1;
}

int cam_grab(Camera *cam, void **data_out, size_t *len_out, int *buf_idx)
{
    fd_set fds;
    FD_ZERO(&fds);
    FD_SET(cam->fd, &fds);
    struct timeval tv = { .tv_sec = 2, .tv_usec = 0 };
    int r = select(cam->fd + 1, &fds, NULL, NULL, &tv);
    if (r <= 0) {
        int saved_errno = errno;
        fprintf(stderr, "[cam] grab select %s\n",
                r == 0 ? "timeout" : strerror(errno));
        if (r < 0 && device_gone_error(saved_errno)) return -2;
        return -1;
    }

    struct v4l2_buffer buf;
    struct v4l2_plane  planes[1];
    memset(&buf, 0, sizeof(buf));
    memset(planes, 0, sizeof(planes));

    if (cam->use_mplane) {
        buf.type     = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        buf.memory   = V4L2_MEMORY_MMAP;
        buf.m.planes = planes;
        buf.length   = 1;
        if (xioctl(cam->fd, VIDIOC_DQBUF, &buf) < 0) {
            int saved_errno = errno;
            perror("[cam] VIDIOC_DQBUF");
            return device_gone_error(saved_errno) ? -2 : -1;
        }
        *data_out = cam->bufs[buf.index].start;
        *len_out  = planes[0].bytesused;
    } else {
        buf.type   = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        if (xioctl(cam->fd, VIDIOC_DQBUF, &buf) < 0) {
            int saved_errno = errno;
            perror("[cam] VIDIOC_DQBUF");
            return device_gone_error(saved_errno) ? -2 : -1;
        }
        *data_out = cam->bufs[buf.index].start;
        *len_out  = buf.bytesused;
    }

    cam->last_timestamp_us =
        (uint64_t)buf.timestamp.tv_sec * 1000000ull +
        (uint64_t)buf.timestamp.tv_usec;
    cam->last_sequence = buf.sequence;
    cam->last_flags = buf.flags;
    *buf_idx = buf.index;
    return 0;
}

int cam_release(Camera *cam, int buf_idx)
{
    struct v4l2_buffer buf;
    struct v4l2_plane  planes[1];
    memset(&buf, 0, sizeof(buf));
    memset(planes, 0, sizeof(planes));
    buf.index  = buf_idx;
    buf.memory = V4L2_MEMORY_MMAP;
    if (cam->use_mplane) {
        buf.type     = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
        buf.m.planes = planes;
        buf.length   = 1;
    } else {
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    }
    if (xioctl(cam->fd, VIDIOC_QBUF, &buf) < 0) {
        perror("[cam] cam_release VIDIOC_QBUF");
        return -1;
    }
    return 0;
}

void cam_close(Camera *cam)
{
    if (cam->fd < 0) return;
    enum v4l2_buf_type type;
    type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    xioctl(cam->fd, VIDIOC_STREAMOFF, &type);
    type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    xioctl(cam->fd, VIDIOC_STREAMOFF, &type);
    for (int i = 0; i < cam->buf_count; i++)
        if (cam->bufs[i].start && cam->bufs[i].start != MAP_FAILED)
            munmap(cam->bufs[i].start, cam->bufs[i].length);
    close(cam->fd);
    cam->fd = -1;
    printf("[cam] closed\n");
}
