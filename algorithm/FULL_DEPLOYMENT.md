# 完整算法板应用部署

前提是已经刷好匹配的厂家 RK3588 镜像，相机媒体链路、MPP/RGA、NPU 驱动、USB 串口等底层功能由厂家镜像提供。本入口安装现在已验证的应用程序、模型、匹配动态库和启动配置；它不替换内核/驱动，也不重新编译算法。

推荐使用仓库统一安装入口的 **完整算法板** 角色，先检查/挂载数据分区、设置日志容量上限，再执行本安装器。`algorithm/install.py` 保留为 **纯视频** 入口，不含检测跟踪。

## 离线运行包

完整发布包包含 `runtime/uav-algorithm-runtime-v1.6.0.tar.gz`，摘要固定在 `algorithm/runtime-release.json`。运行包根目录含 `runtime-manifest.json`；每个文件有 SHA256、大小、权限，安装前既检查外层压缩包摘要，也检查解压后的每个文件。不允许路径穿越、软链接、硬链接、特殊文件、未列入清单的文件。

运行包包括：

- `stream/rk_streamer`：包含算法画框、共享帧回传和按需低清分支的实际推流程序。
- `stream/tracker_worker`：已静态链接算法库的实际检测跟踪进程。
- `stream/mino17_streamer`：独立红外推流程序。
- `stream/TTTracker_V1.1_publish/model/`：与现程序匹配的检测和搜索模型。
- `stream/TTTracker_V1.1_publish/libs/`：已验证的 OpenCV、JPEG/Jasper、RKNN 动态库。

`.a` 是编译输入，不是新板的运行依赖，不放入这个运行包。运行包仅针对同类 RK3588 BSP；安装会用 ELF 架构和 `ldd` 检查实际依赖，不把“文件复制成功”当作驱动兼容验证。

## 直接调用（已先准备数据盘与公共日志配置）

```bash
sudo bash deploy.sh --role algorithm --doctor
sudo bash deploy.sh --role algorithm
```

无摄像头也能安装，三个服务各自等待自己的相机；不会因为某一路缺席阻塞其他路。默认自动寻找真正挂载的 `/data` 或 `/mnt/data`，拒绝把完整模型部署在系统根分区。可以通过 `--data-root /mnt/data` 指定。

安装默认不联网。完整厂家镜像若缺少 `v4l2-ctl`、`arping` 等基础命令，准备相应系统软件包后再运行，或在有网络时显式加 `--install-deps`。

默认板间网络为 `eth0`、`192.168.10.2/24`，向主控 `192.168.10.1` 发送。参数可以使用 `--iface`、`--address`、`--host` 修改。兼容已有静态 NetworkManager 配置；新建配置时不设置网关/DNS，保留原连接用于失败回滚。不修改 Wi-Fi；所选网口如果承载默认路由或任何当前 SSH 连接，会拒绝配置，避免安装中断远程连接。

`--no-start` 仅安装并启用下次开机自启，不启动应用、不切换当前网络。相同运行包、安装器和配置重复执行不会重启服务。发现旧应用、同名服务、相机占用、TCP 9000 占用或文件被改动时停止并说明原因；不自动杀进程或覆盖旧版。

## 服务和性能配置

| 服务 | 内容 | 默认参数 |
|---|---|---|
| `uav-front-algorithm.service` | 检测跟踪 + 可见光算法视频 | 1920×1080，30fps，5000kbps，GOP15，旋转 0° |
| `uav-front-preview.service` | 另一前视相机原始预览 | 1920×1080，30fps，3000kbps，GOP15，旋转 0° |
| `mino17-streamer.service` | Mino17 红外 | 1500kbps，旋转 0°，相机缺席每 30 秒检查 |
| `uav-algorithm-network.service` | 独立板间网络 | 不添加默认路由 |

算法采集/编码固定在 CPU 4、5，算法 worker 使用 CPU 6、7，保持 OpenCV 线程池为 8；低清分支使用 CPU 0～3。算法处理与高/低清按需推流使用同一套已验证程序，没有在部署中修改算法运算逻辑。

服务日志写入 journald，每路限制 30 秒 200 条；统一安装工具负责将系统日志持久化到数据盘并设置总容量上限。服务名沿用地面站维护界面识别的名称。

## 日常操作

```bash
sudo uav-algorithm status
sudo uav-algorithm addresses
sudo uav-algorithm restart algorithm
sudo uav-algorithm restart preview
sudo uav-algorithm restart infrared
sudo uav-algorithm stop all
sudo uav-algorithm start all
```

算法板控制入口是 TCP 9000，主控转发给地面站为 TCP 9001。视频分别发送到主控 RTP 5602/5603/5604，主控提供 `/algorithm`、`/preview`、`/infrared`。

安装前网络地址、路由、原厂网络服务保存到 `/var/backups/uav-algorithm-full/时间戳/`。首次安装中出错会停止本次创建的服务、删除本次创建的文件，并恢复本次修改的网络；不删除原厂文件。安装成功后需实际解码三路视频、发送检测跟踪控制指令验收。仅看到服务 `active` 不能证明相机已出图或算法识别正常。
