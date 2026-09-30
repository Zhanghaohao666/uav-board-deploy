# 算法板纯视频一键配置（v1.1.1）

适用于与当前算法板相同的 RK3588 ARM64 厂家 BSP：两路 CR200 MIPI 相机已能提供 1920×1080 NV12，系统具备 MPP/RGA 硬件编码库。USB Mino17 红外可选。**本工具只负责网络、采集、推流和开机自启，不安装检测/跟踪算法，不提供 TCP 9000 控制服务。**

## 新板一条命令

先接好两路可见光相机、板间网线，并让新板能访问 GitHub 下载地址及系统软件源。在新算法板终端运行（普通用户默认 `dev`）：

```bash
bash <(curl -fsSL -H 'Accept: application/vnd.github.raw+json' 'https://api.github.com/repos/Zhanghaohao666/uav-board-deploy/contents/bootstrap.sh?ref=v1.1.1') --repo Zhanghaohao666/uav-board-deploy --ref v1.1.1 --role algorithm --user dev
```

可离线复制**完整仓库/发布包**到新板，再运行：

```bash
sudo bash algorithm/install.sh --user dev
```

离线安装仍须预先具备系统工具 `ip`、`arping`、`v4l2-ctl`、`stdbuf` 及 BSP 库；有网络时安装器只补缺失的用户态工具，不升级内核或 BSP。GitHub API 入口和 codeload 都须可访问。工具不配置联网网关或 NAT。

## 默认配置

| 项目 | 默认值 |
|---|---|
| 算法板板间接口/IP | `eth0` / `192.168.10.2/24` |
| 下视主控目的 IP | `192.168.10.1` |
| 前视 1 节点 | `/dev/v4l/by-path/platform-rkcif-mipi-lvds4-video-index0` |
| 前视 2 节点 | `/dev/v4l/by-path/platform-rkcif-mipi-lvds-video-index0` |
| 可见光输出 | 每路 1920×1080、30 fps、H.264、目标 3000 kbps，默认不旋转 |
| 红外输出 | Mino17 640×512、25 fps、H.264、目标 1500 kbps，默认不旋转 |
| 传输协议 | H.264 over RTP/UDP；接收端在主控上转换成 RTSP |

Mino17 在已接入时按本机 `/dev/v4l/by-id/*Mino17*video-index0` 识别；有多个设备须明确指定。未接红外也允许安装，其服务等待设备出现；若将来接入的 USB 标识不同，须更新配置设备路径。没有红外时可加 `--without-infrared`。

| 算法板视频 | 发送到主控 | 主控 MK22 默认拉流地址 |
|---|---|---|
| 前视 1 原始画面 | `192.168.10.1:5602` | `rtsp://192.168.2.36:8554/algorithm` |
| 前视 2 原始画面 | `192.168.10.1:5603` | `rtsp://192.168.2.36:8554/preview` |
| Mino17 红外 | `192.168.10.1:5604` | `rtsp://192.168.2.36:8554/infrared` |

地址 `/algorithm` 为兼容已有地面站保留，**此角色下没有检测/跟踪框**。主控须已安装其部署程序，并处于 `algorithm` 或 `dual` 模式。两路公共的下视 MIPI、D455 仍由主控采集。纯视频目标码率合计约 7.5 Mbps，实际还有协议开销；板间持续推流，地面站按需拉流。

## 适用范围与参数

- 网络接口须为厂家 BSP 中不受 NetworkManager 管理的有线口（当前算法板符合）；若由 NetworkManager 管理，本版本会拒绝安装并说明，避免与其地址管理冲突。其他自行配置的网络守护程序也不得覆盖这个口的板间地址。
- 两路 MIPI 驱动/媒体链路须已经配置为 1920×1080 NV12。本工具不刷驱动、修改设备树或猜测 media-controller 拓扑。
- IP 是**追加**配置，不清空其他地址/默认路由；同一二层网络不能同时有两块 `192.168.10.2` 算法板。两套独立网络可以复用该地址。要改目的地址，必须同步改主控配置。
- 旧算法或采集程序正在运行/已启用自启时，本工具拒绝抢相机。已有算法需要与推流整合时，应由同一采集链路输出视频，不能同时启动本工具抢同一节点。
- 若有厂家 `downstream-static-proxy.service`，安装器新增 `90-uav-algorithm.conf` 将其下次启动的网络命令指向本工具，保留原文件及其他地址；避免厂家脚本开机清空地址。安装时不重启该厂家服务。

可追加以下参数（GitHub 命令中放在 `--role algorithm` 后）：

```bash
--iface eth0 --address 192.168.10.2/24 --host 192.168.10.1
--front1 /dev/v4l/by-path/platform-rkcif-mipi-lvds4-video-index0
--front2 /dev/v4l/by-path/platform-rkcif-mipi-lvds-video-index0
--front1-rotate 90 --front2-rotate 270
--infrared-rotate 180
--without-infrared
--no-start
```

前视允许 0/90/180/270 度，红外允许 0/180 度。90/270 度旋转后画幅变为 1080×1920，不会无损变成另一个横向视野。当前旧板采用前视 1 旋转 90、前视 2 旋转 270；新板默认 0，请按实际安装方向选择。

`--no-start` 仅安装并启用下次开机，不立即配置 IP 或启动推流。普通安装需要接通板间网线以检查 IP 冲突；只有工厂 BSP、依赖和当前相机格式预检查通过才继续。相同版本同配置再次运行不重启服务；不同版本、不同配置或安装文件损坏时拒绝覆盖，须先安排维护。本版本不提供自动升级旧算法程序。

## 开机过程与状态检查

系统开机 → `uav-algorithm-network.service` 追加板间 IP/检查冲突 → 各自启动 `uav-algorithm@front1`、`@front2`、`@infrared`。相机缺席每 5 秒重试；连续 60 秒没有有效帧率报告时重启本路。三路独立，不运行跟踪、模型加载或云台控制。

```bash
sudo uav-algorithm status
systemctl is-enabled uav-algorithm-network.service uav-algorithm@front1.service uav-algorithm@front2.service uav-algorithm@infrared.service
journalctl -u uav-algorithm@front1.service -n 60 --no-pager
journalctl -u uav-algorithm@infrared.service -n 60 --no-pager
```

配置保存在 `/opt/uav-algorithm/config.json`。修改单路旋转/码率后，检查配置有效，再重启对应一路，例如 `sudo systemctl restart uav-algorithm@front1.service`。改 IP 涉及两端配置，需在维护时操作。

只读预检查（不打开采集流、不改 IP、不停服务）：

```bash
sudo bash algorithm/install.sh doctor --user dev
```

有相机占用或旧自启服务时 doctor 返回非零并说明原因。这表示**不能直接安装第二套采集**，不代表现有摄像头或串流程序故障。

## 验收与验证范围

43 项 Python 自动测试通过（包含原主控 29 项）：覆盖端口/纯视频模式、设备别名冲突、IP/旋转参数、保留其他地址、重复 IP、网口子网冲突、USB 可选格式、红外缺席、旧服务及手动相机占用拒绝、无帧重试、安装回退、仅开机启用、重复安装与文件损坏检测。Mino17 解包 C 测试通过。

2026-09-30 在现有算法板做了只读检查：两路前视分别解析到 `/dev/video22`、`/dev/video0`，格式符合，动态库及程序 `--help` 检查通过；当时红外未接入。系统缺少 `arping`，检查时仅从审查目录临时提供该工具，未安装系统包、未发送 ARP，正式新板安装时会补齐该依赖。检测到旧的开机采集服务和手动运行的 `rk_streamer`，因此 doctor 正确拒绝第二套采集安装；原采集进程保持运行。

新板安装后的现场验收：

1. 检查 IP、到主控的路由及各服务日志；红外未接时其重试属预期。
2. 用主控或地面站**实际解码**三路 RTSP，确认画面、方向及帧率。服务 active、发送帧率正常并不保证接收端成功解码。
3. 断电重启新板，再确认自动恢复；拔插红外验证独立恢复。

当前开发验证没有替换或停止旧算法板的检测程序，也没有在其忙碌的相机上抢流。**新板实装、三路同时解码和断电自启仍需现场验证，不能据此声称已完成。**

## 云台新板

同型号 RK3576 云台若已具备相同 BSP、MPP/RGA/相机驱动、运行库、IP `192.168.144.108` 和启动脚本，只需换成支持双路的新程序。全新空系统仅拷贝可执行文件还不够；本算法板安装器不能用于云台。云台继续使用自身 `/etc/init.d/S99hust_tracker`，可见光和红外分别输出 RTSP `/live/0`、`/live/1`。
