# E1X 车载激光雷达按需录制

地面站“数据录制”中的雷达区域可以独立开始、停止 E1X 原始点云录制。数据保存在主控 `/data/uav-lidar/日期时间_随机标识/`，使用厂家 `.inno_pc` 原始格式，可用对应 Seyond SDK 离线回放、转换为 PCD。它不是视频录像，也不是 WT1500A 串口测距。

默认接法：主控 J16 / `eth1` → E1X，雷达地址 `172.168.1.10:8010`。工具不改网络地址、不占用相机、不通过 MK22 传输点云；地面站只发送开始/停止命令和读取状态。雷达未接好时显示供电/链路/路由原因，不循环重启采集。

## 安装与升级

全量新板安装包含 API、录制脚本和服务。已有主控运行：

```bash
sudo python3 install_lidar_recorder.py --upgrade
```

升级前原文件备份到 `/var/backups/uav-lidar-installer/`，只重启录像控制 API，不重启视频服务。原连接凭据和 SDK 保留不变。当前有雷达采集时拒绝升级。视频 API 保持 `api_version: 1`，新状态增加可选 `lidar` 对象，旧地面站继续使用视频录制。

**厂家 SDK 需另行安装**，默认 `/usr/local/bin/seyond-get-pcd`。本安装器不下载、编译或替换厂商 SDK。E1X 需要使用已经验证兼容其 TCP 初始化及角度表的版本。未安装 SDK 时功能显示缺少工具。

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
- SDK 标准版没有温和停止信号处理。完成轮转的段保留；停止时最后一段可能不完整，`session.json` 明确标为 `complete: false`。
- 如果另装经过验证的温和停止版 SDK，停止会等帧边界并排空写入缓存，记录 `closed_at_frame_boundary: true`。即使可回放，提前停止的最后分段也不会伪装成录满的分段。
- 采集日志每会话最多保留约 2 MiB。连续 12 秒无点云或超过一个分段时长仍未落盘，停止并保留已有文件。突然断电最后一段也可能不完整。

## 可选板端配置

管理员可创建 `/etc/uav-lidar.json`（没有文件则使用默认值），例如：

```json
{
  "lidar_ip": "172.168.1.10",
  "interface": "eth1",
  "port": 8010,
  "root": "/data/uav-lidar",
  "sdk": "/usr/local/bin/seyond-get-pcd",
  "segment_seconds": 60,
  "max_seconds": 86400
}
```

保存位置必须位于同一 `/data` 数据盘，分段范围 5～300 秒，最长单次采集不超过 86400 秒。地面站不允许 HTTP 请求传入路径、命令或雷达地址。

HTTP 使用既有录像 API 的身份认证：`POST /v1/lidar/start`、`POST /v1/lidar/stop`，JSON body 只能为 `{}`。`GET /v1/status` 返回 `lidar` 中的 `phase`、`active`、`available`、`detail`、`error`、`path`、`bytes`、`started_at`、`lidar_ip` 和 `interface`。`available` 表示本机链路、路由、磁盘和工具满足启动条件，实际设备应答和写盘在开始后验证。

独立正常停止工具的补丁、离线构建和已测范围见 [SDK 录制工具说明](../tools/seyond/README.md)。
