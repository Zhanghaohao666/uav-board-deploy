# RTSP 缺失输入隔离修复

源代码：rtsp_relay_mino17.c。沿用原 RTP→RTSP 管道、shared 工厂及红外时间戳恢复逻辑，为不同客户端提供独立的请求处理线程，避免没有数据的 DESCRIBE 占住其他客户端的请求处理。

API 依据：https://gstreamer.freedesktop.org/documentation/gst-rtsp-server/rtsp-thread-pool.html

在兼容的 RK3588 BSP 上编译：

```sh
gcc -O2 -Wall -Wextra rtsp_relay_mino17.c -o rtsp_relay_mino17 $(pkg-config --cflags --libs gstreamer-rtsp-server-1.0)
```

回归测试在独立 TCP 18554、UDP 25702/25602 端口运行，使用小尺寸合成画面，不占用真实相机。需要 ffmpeg 的 libx264 编码器：

```sh
python3 tests/rtsp_missing_source.py --old-bin /path/to/old/rtsp_relay_mino17 --fixed-bin /path/to/fixed/rtsp_relay_mino17
```

104 实测旧版在缺失源请求后正常路解码失败；新版正常路仍能解码。实际四路验证覆盖缺失三路请求前、中、后。服务 active 不能代替画面验证。

## RK3588 RGA 安全提交（v1.7.1）

`rga_safe.c/.h` 是颜色转换、旋转和按需低清共用的实现。只从受限 DMA32 heap 分配并 `importbuffer_fd()`，使用导入句柄提交，按调用线程指定 `IM_SCHEDULER_RGA3_CORE0 | IM_SCHEDULER_RGA3_CORE1`。CPU 读写执行 DMA_BUF_SYNC；内部宽高按 16 对齐，外部可见尺寸不变，NV12 UV 偏移按垂直 stride 处理。

每线程最多缓存 8 个缓冲，复用时不反复导入，低清租约结束/线程退出释放；校验/提交出错即阻止该线程继续提交，无 RGA2 或虚拟地址回退。目标为 RK3588 当前 BSP，不用于 RK3576 云台。

`make rk_streamer` 生成纯视频主控版本；算法版本还须将完全匹配的 `tracker_tt.cpp`/IPC 头和 OpenCV 3.4.15 链接到同一组对象，不能用纯视频二进制覆盖算法板。Mino17 从 `algorithm/mino17-source` 构建，也链接共用的安全提交模块。

主机失败路径测试：`python3 -m unittest discover -s tests -p test_rga_safe.py -v`。真机合成图像测试须使用目标 BSP 头文件/库，将 `tests/rga_safe_probe.c`、`rga_safe.c` 和 `rga_convert.c` 一起编译。它不打开相机、不监听推流端口，验证可见图像、颜色/旋转、资源释放和缩放耗时。
