# RK3588 主控与算法板完整一键部署

v1.6.0 用于**已经刷好同型号厂家完整 RK3588 ARM64 BSP 的新板**。不替代烧录镜像，不替换内核或相机驱动，也不用于 RK3576 云台。

## 在线安装

在新板终端执行（保留终端供交互选择）：

```bash
curl -fL https://raw.githubusercontent.com/Zhanghaohao666/uav-board-deploy/v1.6.0/bootstrap.sh -o /tmp/uav-bootstrap.sh && bash /tmp/uav-bootstrap.sh --repo Zhanghaohao666/uav-board-deploy --ref v1.6.0
```

选择“主控板”或“完整算法板”，再按提示选择网口与部署方案。也可在命令末尾添加 `--role main` 或 `--role algorithm`。

| 安装角色 | 安装内容 |
| --- | --- |
| 主控板 | 下视 MIPI、D455i 彩色视频、载荷视频转发、控制转发、单路/多路预览支持、视频和雷达按需录像接口、方案切换及开机自启 |
| 完整算法板 | 已验证的检测跟踪程序、所需模型与动态库、两路前视及 Mino17 红外独立推流、低码率预览分支、固定板间 IP 与独立自启服务 |

算法运行包由安装器下载并校验 SHA256；不需要手动复制静态库或重新编译算法。运行程序已经链接算法库，安装器不修改算法逻辑。仅需原始视频的特殊场景仍可使用 `--role algorithm-video`，它不包含检测跟踪。

## 网口与存储

- 主控支持算法板单网口、云台单网口、双网口并行测试；根据实际接线选择，不能照抄其他板子的 USB 网卡名。
- 主控 MK22 默认地址 `192.168.2.36/24`；算法板板间地址默认 `192.168.10.2/24`，主控对应 `192.168.10.1/24`。同一网络多块主控必须使用不同地址。
- 雷达默认使用专用 J16/`eth1`，主机地址 `172.168.1.100/24`、雷达 `172.168.1.10`。安装器保留该接口，禁止与 MK22/载荷网口重复分配。型号映射不同可指定 `--lidar-iface`；不用雷达可加 `--without-lidar`。
- 雷达采集工具随包提供，**不需要用户另装厂家 SDK**。开机只配置网络，不自动采集；插好、供电后由地面站按需开始/停止录制。
- 优先复用独立数据盘。尚未挂载时可明确选择已有 ext4 分区 UUID；不格式化，不把系统盘目录冒充数据盘。主控录像默认 `/data/uav-recordings`，雷达默认 `/data/uav-lidar`。
- 日志迁到数据盘，设置容量限制与每 10 分钟轮转。保留原日志；发现仍被旧进程持有的日志会单独处理或报告。

## 本地及离线安装

从 [v1.6.0 发布页](https://github.com/Zhanghaohao666/uav-board-deploy/releases/tag/v1.6.0) 下载 `uav-board-deploy-offline-v1.6.0.tar.gz`，其中包含脚本、雷达工具和算法运行包：

```bash
tar -xzf uav-board-deploy-offline-v1.6.0.tar.gz
cd uav-board-deploy
sudo bash deploy.sh --offline
```

离线模式不访问软件源；若厂家镜像缺少必要系统软件包，会列出缺项并停止，需要提前联网补齐。离线包并不是完整操作系统镜像。

普通源码包 `uav-board-deploy.tar.gz` 不包含算法运行归档；可联网使用 `sudo bash deploy.sh`，或另下载同一版本的 `uav-algorithm-runtime-v1.6.0.tar.gz` 放进 `runtime/`。不用直接运行旧 `install.sh` 代替完整部署入口。

## 检查与日常维护

```bash
sudo bash deploy.sh --role main --doctor
sudo bash deploy.sh --role algorithm --doctor
sudo uav-switch
sudo uav-algorithm status
```

前两项为预检查；后两项分别在安装完成的主控、算法板运行。主控方案菜单可查看状态、视频地址及重启服务；算法板可用 `sudo uav-algorithm restart algorithm` 重启检测跟踪。

`--no-start` 保存应用自启配置但本次不启动应用；存储和日志准备立即生效。相机缺席时对应服务等待，不连带停止其他路。已有部署会检查安装记录和配置；不兼容的旧部署拒绝覆盖，避免抢相机和端口。

## 验证范围与详细文档

已做安装逻辑、冲突保护、回滚及独立服务自动化测试，并在现有 ARM64 板只读核对算法运行包依赖和雷达工具。没有对现用板执行完整重装；新镜像首次安装后，仍应逐路出图并重启验收。风扇和传感器驱动属于厂家镜像，不能凭安装成功断言硬件全部正常。

- [完整算法安装细节](algorithm/FULL_DEPLOYMENT.md)
- [算法视频与兼容工具](algorithm/README.md)
- [视频低码率预览](docs/adaptive-streams.md)
- [雷达按需录制](docs/lidar-recording.md)
- [历史主控底层安装入口](docs/legacy-main-install.md)（保留参考，新板优先使用本页 `deploy.sh`）
- [版本变更](CHANGELOG.md)
- [配套地面站](https://github.com/Zhanghaohao666/uav-ground-station)
