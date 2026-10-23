# E1X 车载激光雷达按需录制

地面站“数据录制”中的雷达区域可以独立开始、停止 E1X 原始点云录制。数据保存在主控 `/data/uav-lidar/日期时间_随机标识/`，使用厂家 `.inno_pc` 原始格式，可用对应 Seyond SDK 离线回放、转换为 PCD。它不是视频录像，也不是 WT1500A 串口测距。

默认接法：主控 J16 / `eth1` → E1X，雷达地址 `172.168.1.10:8010`。完整部署会为明确选定的雷达专用网口设置 `172.168.1.100/24` 和网络自启；录制过程不改网络、不占用相机、不通过 MK22 传输点云，地面站只发送开始/停止命令和读取状态。雷达未接好时显示供电/链路/路由原因，不循环重启采集。

## 安装与升级

在匹配型号的厂家完整 ARM64 镜像上，全量新板部署包含 API、录制脚本、经过验证的独立采集工具和雷达网络自启。无需在新板下载、安装整套 SDK 源码或编译。相同 SHA256 的既有正常停止版工具会复用；否则安装到独立的 `/opt/uav-lidar-runtime/<摘要>/`，保留旧 SDK。

雷达工具独立预检/安装入口（由完整部署入口调用，一般无需单独执行）：

```bash
sudo python3 tools/seyond/install_runtime.py --interface eth1 --check
sudo python3 tools/seyond/install_runtime.py --interface eth1
```

`--check` 只读检查，不改变网络，不接触雷达设备；`--no-start` 安装网络自启但不立即配置网口。安装前检查 ARM64、SHA256、SDK 版本、动态库以及原有接口用途，拒绝占用板间/MK22/Wi-Fi/默认路由接口，拒绝雷达网段与其他网口重叠。已有同网段静态 NetworkManager 连接会复用；新连接有独立配置，不覆盖厂家连接、不更改其他接口或默认网关。没有插线、雷达未供电也能完成安装。

厂家当前 NetworkManager 镜像支持自动配置。不同网络管理器或已有冲突静态配置会明确报错，不能把 `eth1` 这个名字当成所有板子的 J16；请按同型号板卡接口定义选择专用口。`/data` 数据盘必须已挂载才能开始录制，安装器不会格式化磁盘或把点云写到系统盘。

已有主控若只需升级录制控制 API，运行：

```bash
sudo python3 install_lidar_recorder.py --upgrade
```

升级前原文件备份到 `/var/backups/uav-lidar-installer/`，只重启录像控制 API，不重启视频服务。原连接凭据和 SDK 保留不变。当前有雷达采集时拒绝升级。视频 API 保持 `api_version: 1`，新状态增加可选 `lidar` 对象，旧地面站继续使用视频录制。

上述 `install_lidar_recorder.py --upgrade` 是控制层的增量升级入口，保留当前 SDK 和网络不变。新板完整部署还会运行配套工具安装步骤，将 `/etc/uav-lidar.json` 的 `sdk` 指向已验证工具。工具保留 E1X TCP 初始化修复、角度表支持和正常停止补丁，厂家许可证随安装包分发。

`uav-lidar-network.service` 只设置专用网口，可开机自启；`uav-lidar-record.service` 始终按需启动，**不开机采集、不自动续录**。两者不是同一个服务。

## 本地命令

```bash
uav-lidar-record start
uav-lidar-record status
uav-lidar-record stop
uav-lidar-record 60
```

数字秒数兼容旧工具，等待该次采集结束；`start` 由 systemd 持有，关闭 SSH 或地面站继续采集。无 `[Install]`，不启用开机采集，重启不会自动续录。`start` 默认最多 24 小时；达到上限停止，需要手动再次开始。

## 存储和停止语义

- SDK 以同一个连接按约 60 秒的帧数分段；每个 E1X compact `.inno_pc` 分段由厂家 SDK 写入对应角度表。实际分段时长以雷达有效输出帧数为准。
- `/data` 必须已挂载；剩余空间不足 10 GiB 停止，历史文件不会自动删除。
- 状态需有完整的有效点云包写入，不能仅由进程存在或角度表判断“正在录制”。SDK 自带 2 MiB 缓冲，低回波场景的文件大小可能分批增长；收到数据、尚未落盘时显示等待 SDK 写盘。
- 完整部署默认使用经过验证的正常停止版工具，停止会等帧边界并排空写入缓存，记录 `closed_at_frame_boundary: true`。即使可回放，提前停止的最后分段也不会伪装成录满的分段，`session.json` 中仍是 `complete: false`。
- 如果管理员手动改回原版 SDK，提前停止时最后分段可能不完整。
- 采集日志每会话最多保留约 2 MiB。连续 12 秒无点云或超过一个分段时长仍未落盘，停止并保留已有文件。突然断电最后一段也可能不完整。

## 可选板端配置

管理员可创建 `/etc/uav-lidar.json`（没有文件则使用默认值），例如：

```json
{
  "lidar_ip": "172.168.1.10",
  "interface": "eth1",
  "port": 8010,
  "root": "/data/uav-lidar",
  "sdk": "/opt/uav-lidar-runtime/0e9531de5c2cafc6/get_pcd_record",
  "segment_seconds": 60,
  "max_seconds": 86400
}
```

保存位置必须位于同一 `/data` 数据盘，分段范围 5～300 秒，最长单次采集不超过 86400 秒。地面站不允许 HTTP 请求传入路径、命令或雷达地址。

HTTP 使用既有录像 API 的身份认证：`POST /v1/lidar/start`、`POST /v1/lidar/stop`，JSON body 只能为 `{}`。`GET /v1/status` 返回 `lidar` 中的 `phase`、`active`、`available`、`detail`、`error`、`path`、`bytes`、`started_at`、`lidar_ip` 和 `interface`。`available` 表示本机链路、路由、磁盘和工具满足启动条件，实际设备应答和写盘在开始后验证。

独立正常停止工具的补丁、离线构建和已测范围见 [SDK 录制工具说明](../tools/seyond/README.md)。
