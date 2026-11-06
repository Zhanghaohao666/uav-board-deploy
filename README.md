# 无人机主控、算法板、云台三套部署

当前交付版本：**v1.7.0（2026-11-06）**。面向已经烧录同型号厂家完整镜像的设备，安装应用、模型、运行库、网络配置和开机启动。不会刷写内核、格式化硬盘或克隆硬件身份。

| 独立安装包 | 设备及内容 | 离线安装入口 |
| --- | --- | --- |
| `uav-mainboard-deploy-v1.7.0.tar.gz` | RK3588 主控：下视 MIPI、D455i、视频/控制转发、高清/低清、板端视频和 E1X 点云录制、日志、方案切换与自启 | `sudo bash install-main.sh --offline` |
| `uav-algorithm-deploy-v1.7.0.tar.gz` | RK3588 算法板：当前检测跟踪程序、模型和动态库，两路前视和 Mino17 红外、按需低清、静态板间 IP 与独立自启 | `sudo bash install-algorithm.sh --offline` |
| `uav-gimbal-deploy-v1.7.0.tar` | RK3576 云台：新旧硬件对应程序、模型和运行库，可见光/红外、按需低清、TCP 控制、原厂/华科切换与自启 | root 登录后 `sh install.sh --profile auto` |

三套包及校验值在 [v1.7.0 发布页](https://github.com/Zhanghaohao666/uav-board-deploy/releases/tag/v1.7.0)。运行包直接包含所需业务程序；新板不需要重新编译算法。

## 在线一键安装

主控板执行：

```bash
curl -fL https://raw.githubusercontent.com/Zhanghaohao666/uav-board-deploy/v1.7.0/bootstrap.sh -o /tmp/uav-bootstrap.sh && bash /tmp/uav-bootstrap.sh --repo Zhanghaohao666/uav-board-deploy --ref v1.7.0 --role main
```

算法板执行：

```bash
curl -fL https://raw.githubusercontent.com/Zhanghaohao666/uav-board-deploy/v1.7.0/bootstrap.sh -o /tmp/uav-bootstrap.sh && bash /tmp/uav-bootstrap.sh --repo Zhanghaohao666/uav-board-deploy --ref v1.7.0 --role algorithm
```

云台执行（以 root 登录；原厂 Buildroot 不需要 Python）：

```sh
wget -O /tmp/uav-gimbal-bootstrap.sh https://raw.githubusercontent.com/Zhanghaohao666/uav-board-deploy/v1.7.0/gimbal/bootstrap.sh && sh /tmp/uav-gimbal-bootstrap.sh --profile auto
```

云台也可使用通用 `bootstrap.sh --repo Zhanghaohao666/uav-board-deploy --ref v1.7.0 --role gimbal`，该角色在 Python 检查之前转入独立 shell 安装器。下载和校验失败不启动安装。无法联网的设备请在电脑下载对应完整包，再上传板子解压安装。

## 离线安装

先校验发布页 `SHA256SUMS`，各包解压后进入对应目录运行：

```bash
# 主控
 tar -xzf uav-mainboard-deploy-v1.7.0.tar.gz
 cd uav-mainboard-deploy
 sudo bash install-main.sh --offline
```

算法板同理解压算法包，运行 `sudo bash install-algorithm.sh --offline`。算法包已经内置算法运行归档。主控包已内置 E1X 厂家采集工具，无需另外下载 SDK 源码。

云台使用未压缩 tar，以兼容不带 gzip 的厂家固件：

```sh
tar -xf uav-gimbal-deploy-v1.7.0.tar
cd uav-gimbal-deploy
sh install.sh --profile auto --check
sh install.sh --profile auto
```

云台 `--profile old` 对应 IMX290/CamID 2 + Mino17，`--profile new` 对应 IMX214/CamID 0 + HIK 红外。保留本机原厂 IQ、硬件证书、序列号、SSH 主机密钥和已有标定。未知型号先停止并检查，不能把其他 SoC 或相机强行当成这些型号。

离线安装不会访问 apt 源；厂家镜像缺少系统依赖时会列出缺项。可提前联网补齐依赖，再离线安装。源码压缩包 `uav-board-deploy.tar.gz` 不含算法/云台大运行库，不能代替三套完整包。

## 接线、地址和存储

- 主控支持算法板单网口、云台单网口、双网口并行。主控板间口默认 `eth0`，安装时按实际网卡选择，不能照抄旧板的 USB 网卡名。
- MK22：主控 `192.168.2.36/24`；算法板：`192.168.10.2/24`，主控对应 `192.168.10.1/24`。
- 云台：`192.168.144.108/24`，主控对应 `192.168.144.1/24`。同 IP 的两台云台不可直接并入同一个二层网络。
- E1X：专用 J16/`eth1`，主机 `172.168.1.100/24`，雷达 `172.168.1.10:8010`。该端口不与 MK22/载荷共用。可指定 `--lidar-iface`；不用雷达时主控加 `--without-lidar`。
- 必须准备独立数据盘；安装器不格式化。主控 `/data/uav-recordings` 保存视频，`/data/uav-lidar` 保存点云，算法程序/模型/日志位于选定数据盘，云台运行库及限容日志位于 `/data/uav-gimbal`。
- 录像、点云均按需启动，开机不会自动录制。缺席相机独立等待，不阻止其他路。

## 一键切换和日常维护

主控本机或地面站 SSH 终端：

```bash
sudo uav-switch
```

菜单：1 算法板单口，2 云台单口，3 双网口并行，4 当前状态和流地址，5 逐路出图验证，6 保存开机默认。切换方案需按实际接线确认；查看状态不停止视频。地面站方案按钮只切拉流地址，不替代板端网络/服务模式切换。

电脑端同一工具在 `pc/`，另提供 `uav-pc-switch-v1.7.0.zip`：安装 Python/paramiko 后运行 `python start_full_link.py`，可指定主控 IP、查看状态、切方案和保存开机模式。它不会重新启动旧版整链路转发脚本。

算法板：`sudo uav-algorithm status`、`sudo uav-algorithm restart algorithm`。云台：`uav-gimbal status`、`uav-gimbal restart`、`uav-gimbal hust`、`uav-gimbal factory`。云台可见光 `/live/0`、红外 `/live/1` 均位于 RTSP 554，控制 TCP 9000。

## 版本、核验与已知问题

主控/算法板程序从 2026-11-06 现场文件核对；本次算法运行归档包含更新后的 `tracker_worker`，不是重复使用旧归档。新旧云台分别保留已验证的二进制及模型/依赖校验值。设备凭据、录像、激活签发私钥和硬件证书不进入公开包。

验证包括安装器回归、备份与回退、包内 SHA256、真实 ARM64 依赖预检及当前设备既有出图记录。没有对正在使用的主控/算法板执行破坏性重装，也没有把模拟文件系统安装当作新硬件整机验收。

**当前算法板在本次发布前出现过内核内存/页表异常和 RGA 提交失败，曾导致板间失联，断电重启后视频恢复；本次归档期间在无人操作、持续供电时再次失联，具体触发原因尚未确定。此版本归档现场程序和安装环境，没有更换/修复厂家内核，不能承诺安装包解决该内核故障。** 新设备安装后需逐路出图、重启及持续运行验收；新云台转动方向、速度、限位与重捕获效果仍须实物标定。

详细文档：[算法板](algorithm/FULL_DEPLOYMENT.md)、[云台](gimbal/README.md)、[低清预览](docs/adaptive-streams.md)、[雷达录制](docs/lidar-recording.md)、[发布验证记录](docs/validation-v1.7.0.md)、[版本记录](CHANGELOG.md)、[配套地面站](https://github.com/Zhanghaohao666/uav-ground-station)。
