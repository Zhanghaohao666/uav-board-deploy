# RTSP 缺失输入隔离修复

源代码：rtsp_relay_mino17.c。沿用原 RTP→RTSP 管道、shared 工厂及红外时间戳恢复逻辑，为不同客户端提供独立的请求处理线程，避免没有数据的 DESCRIBE 占住其他客户端的请求处理。

API 依据：https://gstreamer.freedesktop.org/documentation/gst-rtsp-server/rtsp-thread-pool.html

在兼容的 RK3588 BSP 上编译：

```sh
gcc -O2 -Wall -Wextra rtsp_relay_mino17.c -o rtsp_relay_mino17 $(pkg-config --cflags --libs gstreamer-rtsp-server-1.0)
```

回归测试在独立 TCP 18554、UDP 15702/15602 端口运行，使用小尺寸合成画面，不占用真实相机。需要 ffmpeg 的 libx264 编码器：

```sh
python3 tests/rtsp_missing_source.py --old-bin /path/to/old/rtsp_relay_mino17 --fixed-bin /path/to/fixed/rtsp_relay_mino17
```

104 实测旧版在缺失源请求后正常路解码失败；新版正常路仍能解码。实际四路验证覆盖缺失三路请求前、中、后。服务 active 不能代替画面验证。
