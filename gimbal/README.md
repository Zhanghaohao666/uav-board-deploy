# RK3576 云台低清编码适配

`rockit_low_encoder.c` 实现 `board/src/mpp_encoder.h` 的编码接口，使用现有 BSP 的 Rockit/VENC 硬件编码；原可见光/红外通道 0/1 保留，低清独立占用 2～7 中空闲的通道。

与 `board/src/low_stream.c/h`、`rtp_sender.c/h` 一起链接进当前云台工程。在可见光原有算法叠框之后，将 RGB/BGR 画面通过 `low_stream_create_rgb("192.168.144.1",5612,width,height,bgr)` 与 `low_stream_offer` 交给低清工作线程；stride/vstride 按原帧像素跨度填写。红外在原 NV12 解包后使用 `low_stream_create("192.168.144.1",5610,640,512)`，同样 offer/destroy。

不要把 3588 的 `mpp_encoder.c` 同时链接进云台，否则编码接口重复。原算法静态库、模型、跟踪参数不由本目录提供或替换。当前实板使用已核对 SHA256 的 5-UAV-1080P 源码独立构建完成；其他算法版本应在自己的对应源码上接入。

启动环境、RTSP 地址及验证见 [按需低清说明](../docs/adaptive-streams.md)。
