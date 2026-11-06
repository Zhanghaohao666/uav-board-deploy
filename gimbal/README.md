# RK3576 新旧云台完整部署

v1.7.2 面向已烧录匹配厂家 Buildroot 镜像的 RK3576 云台。普通 RK3588 主控/算法板不能使用此安装器。

## 下载与安装

从统一仓库 v1.7.2 发布页下载 `uav-gimbal-deploy-v1.7.2.tar`，上传云台：

```sh
tar -xf uav-gimbal-deploy-v1.7.2.tar
cd uav-gimbal-deploy
sh install.sh --profile auto --check
sh install.sh --profile auto
```

云台原厂环境不要求 Python、systemd 或 gzip。在线入口是本目录 `bootstrap.sh`，固定版本和完整归档 SHA256。源码下载不包含二进制运行库，请使用发布资产。

安装前验证 RK3576、ARM64、init.d 启动结构、独立 /data、空间、文件校验及实际动态加载器。相机匹配失败会停止。`--check` 不启动/停止相机和应用，不修改网络或自启；`--no-start` 安装与启用下次开机，但本次不启动新程序（替换应用时会停止原应用）。

## 两种硬件

| profile | 可见光 | 红外 | 输出 |
| --- | --- | --- | --- |
| old | IMX290，CamID 2 | Mino17 | 可见光 1920x1080/30fps；红外 640x512/25fps |
| new | IMX214，CamID 0，VI 60→30fps | HIK USB 2bdf:0102 | 可见光 1920x1080/30fps；红外 640x512/30fps |

new 的 HIK 原始 UVC 为 640x1032 YUYV，取第 516～1027 行的视频区域；不把 RAW 温度数据和元数据当作图像。对应 4 个源码修改及补丁在 `source/`。算法库未因这次硬件适配改变。

低清 RGB 为 640x360/15fps/650kbps，红外为 320x256/15fps/400kbps。由 192.168.144.1 的请求租约启动，无请求约 5 秒自动停止编码。

## 运行和切换

```sh
uav-gimbal status
uav-gimbal restart
uav-gimbal urls
uav-gimbal factory
uav-gimbal hust
```

应用 `/home/hust_tracker_3576`；RTSP `rtsp://192.168.144.108:554/live/0` 和 `/live/1`；TCP 控制 9000。高清 RTP 发往 192.168.144.1:5612/5610；低清 RTP 6612/6610，请求 UDP 15612/15610。

`/etc/cfg/hust-app-mode` 保存原厂/华科/停止选择。`S98uav_gimbal_network` 仅补充 eth0 的应用地址，保留其他管理地址；`S99hust_tracker` 依照所选模式启动，使用启动锁避免重复运行。重启不需要电脑重新开程序。

原厂库不会覆盖；应用依赖隔离在 `/data/uav-gimbal/releases/版本-profile`，模型通过 `/model` 接入。遇到不一致的既有 `/model` 时安装停止，保留原内容；不静默覆盖其他算法模型。

日志 `/data/uav-gimbal/logs/tracker.log`，后台每分钟检查，超过 16MiB 保留末尾 1MiB 后截断；应用使用 append 模式，避免截断后空洞文件。上一份启动日志为 `.previous`。它是普通 shell 休眠任务，没有持续轮询 CPU。

## 备份和恢复

每次首次安装先保存原应用与启动文件到 `/data/uav-gimbal/backups/日期-进程号/before.tar`，对应目录附 `restore.sh`：

```sh
sh /data/uav-gimbal/backups/实际目录/restore.sh
```

恢复原文件和开机状态，不自动启动原应用。失败事务也会恢复文件并保留失败版本；如需立即运行原程序，由操作者确认后手动启动。相同版本/profile 重复安装不会重启程序，存在不同托管版本时拒绝覆盖。

不复制其他设备证书、SSH host key、序列号、网络 MAC 或原厂 IQ。已有 `recap_servo.env` 保留；没有时提供从实测老机得到的默认值，并明确提示新机需确认方向/速度/机械限位。软件复制不能代替新机械标定。

## 验证边界

新程序此前已在新云台验证双路高清、按需低清、检测计算、被动控制回报和实际重启自动恢复；旧云台保留旧程序继续运行。发布安装器新增了隔离运行库、备份/恢复、模式选择和有容量限制的日志，另做文件系统事务测试与真机只读预检。没有将真机 `--check` 宣称为新固件完整安装/重启验收；原厂 Demo 的实物功能与目标跟随、重捕获转动仍需现场确认。
